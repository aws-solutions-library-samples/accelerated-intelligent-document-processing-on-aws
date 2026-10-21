#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Advisory UX test of one merge request's web UI, posted with a narrated video.

This is the unattended form of ``.claude/skills/ux-test.md``, under the contract in
``.claude/skills/ux-test-ci.md``. For one MR it decides in about a minute whether
there is anything to look at; then it serves the MR's own UI build to a confined
headless Chrome, signs a persona of the long-lived review stack ``idp-ux-nightly``
in, records while Claude Code over Amazon Bedrock drives that browser, renders the
recording as a narrated mp4, and appends the findings and the video to the
``ai_mr_review`` note for the same head, with one reply in its thread.

The CI jobs are ``ux_test_build`` and ``ai_ux_test`` in ``.gitlab-ci.yml``. The
local equivalent is ``make ai-ux-test-local MR=<iid>``, which runs this file inside
the Docker image ``scripts/ux_runner/run_in_docker.py`` builds from the same
``install.sh``, as a dry run.

What this is NOT
----------------
**It is not a gate.** It produces an advisory look at a screen for a human to act
on; both CI jobs are ``allow_failure: true`` and neither is a shared gate. Exit 1
here means the *tooling* failed (the toolchain or the managed policy is missing,
the proxy or Chrome did not start, the session failed, the browser opened a socket
it must not), never that the UX test found something. Same conventions as
``scripts/sdlc/ai_mr_review.py``: a skip is loud (``SKIPPED:`` and why), and
``--fail-on-skip`` turns one into an error.

Properties it is built around
-----------------------------
1. **No MR code runs in this job.** The MR's ``npm ci`` (with its lifecycle
   scripts) and ``vite build`` (with its config) run in ``ux_test_build``, so
   nothing the MR wrote executes beside the signed-in browser, whose DevTools port
   is unauthenticated, or beside the harness driving it. This job receives only
   the static files and serves them itself, through the egress proxy, at
   ``http://localhost:3000``. The harness (this file, ``scripts/ux_runner/``, the
   proxy, the recorder, the flows file and both skills, :data:`HARNESS_PATHS`) is
   extracted from the **target** branch with ``git archive`` into a fresh directory
   and run from there, and the job refuses to run the checkout's own copy.
   ``ui-change.json`` comes from the job that ran MR code, so it is never read
   here: the change is classified again from git (:func:`precheck`). Everything
   it makes is made under umask 022, set in :func:`main` whatever the job's was,
   and the job makes the checkout and the harness writable by root alone and runs
   this file with ``-B``, so the browser's uid can change nothing root reads.
2. **The browser's only network path is the proxy.** Chrome for Testing runs as
   ``uxbrowser`` with flags and a managed policy that send every request to
   ``scripts/ux_test_proxy.py`` (run as ``uxproxy``), which tunnels only to the
   review stack's own hosts by exact name and refuses instance metadata, DevTools
   and private addresses, except a VPC endpoint's for an AWS service name. Two
   self-tests prove the policy is enforced and the traffic arrives at the proxy
   before anything is recorded, and a socket audit by uid covers Chrome's whole
   life: it starts as soon as Chrome is launched, so the sign-in on the MR's own
   login page is inside it, and its last pass runs after Chrome has been terminated,
   which happens as soon as the recorder stops and before anything is rendered or
   posted. Terminating Chrome kills its process group and then, once the uid's
   processes have had a few seconds to exit by themselves as Chrome's own crashpad
   handlers do, every process its uid still runs (``chrome.kill_strays``), so a
   browser process that left the group with ``setsid`` cannot outlive the audit
   either. Nothing is posted unless the audit is clean and no such process was still
   running once that wait was over. The proxy's directory is ``uxproxy``'s, so its
   ready file, refusal log, stats and log are read back the way render's outputs are
   (``egress``, ``readback``): a link, a pipe, another uid's file or an oversized
   one in their place is refused unread. That fails the run for all but the stats,
   which are read only for ``summary.json`` once the run is decided, and are then
   left out with a line saying why, while the refusal log is still copied for the
   note's caveat to point at.
3. **No secret reaches the model.** The persona is signed in over DevTools before
   the recorder starts, so the password reaches neither the model nor the video's
   narration. ``claude`` runs with no built-in tools (``--tools ''``), only the
   browser and recorder tools ``tool_profiles`` grants, ``--permission-mode
   manual``, ``--strict-mcp-config`` and ``--bare``, in an environment built from
   an allowlist that never holds ``GITLAB_REVIEW_TOKEN`` or the personas, with a
   self-assumed AWS session narrowed to Bedrock. When that assume is refused,
   ``claude`` runs on the job's own credentials instead, which a line says before
   the session starts and ``summary.json`` records (``credential_scope.claude``
   is ``ambient``). Render, which decodes the page's frames and speaks the model's
   narration, runs as a child process of its own, as ``uxrender``, with the
   recorder's few variables and a second session narrowed to Polly
   (``recording.render_video``), never in this process; when that assume is
   refused, render holds no credential at all and the video has no narration
   (``credential_scope.render`` is ``unavailable``). The session directory is
   ``uxrender``'s by then, so the mp4 and the chapters this process reads back
   from it are read only as regular files of that uid directly in the directory,
   opened without following a link and bounded in size
   (``recording.copy_output``): a link, a pipe or an oversized file in their place
   fails the run. ``claude``'s MCP servers start under ``env -i``. The prompt goes
   on stdin, and every process runs in its own group, killed on a timeout and on a
   job cancel. Of what ``claude``, render and the recorder's subcommands print,
   this process keeps at most ``processes.MAX_OUTPUT_BYTES`` a stream and reads
   and drops the rest, so a child that floods its output fills neither this
   process's memory nor, through ``render.log``, the job's disk. git is read the
   same way (``ui_change.git_prefix``), so the MR's diff, whatever its size, costs
   the precheck no more than the prompt's budget for it.
4. **It edits only its own notes.** Notes are attributed by the author id ``GET
   /user`` returns for the token, so a marker another user pastes neither
   suppresses a run nor gets edited. Within the token's own notes a marker counts
   only where it is written: the code review's on its note's first line, and this
   job's at the start of a note, under the standalone heading, or under the code
   review's footer. The code review quotes MR content and shares the token, so a
   marker it quotes neither picks the note nor marks the head tested
   (``gitlab_api.review_note_for``, ``section.sections_in``). The model's text
   reaches the note only as the fields of its JSON block, sanitised and bounded
   (``section.parse_report``), with no mention, link, URL, math command, or issue,
   merge request, epic, label, milestone, snippet or iteration reference left
   that GitLab would act on (a bare commit SHA is the one form left as written),
   and every body is defused of quick actions. The composed section is redacted
   once more before it is posted, and that pass cannot write a link either
   (``section.inert_markup``). Every printed line and every artifact goes through
   one redactor, and so does the narration: every mark's spoken line and label is
   redacted in the recording's timeline before render reads it for the voice, the
   captions and the chapters. In CI the artifact directory sits in the MR's
   checkout, so it is refused when it is a symlink, and every artifact is opened
   without following one.
5. **It is idempotent per head SHA.** The section's marker names the full head and
   the prompt revision, and a head that already has one is ``UNCHANGED``. When a
   newer head is posted, older notes' video lines are marked superseded; the
   uploads themselves are kept.
6. **Only the review stack's screens become a video.** The frames are not redacted,
   which is acceptable for :data:`REVIEW_STACK` alone, because it holds only shipped
   example data. So a video is rendered, attached and uploaded only when three
   statements of which stack the run used all name it: ``--stack``, the settings
   parameter ``UX_TEST_UI_ENV`` names, and the stack the signed-in page reports
   (:func:`review_stack_problem`, which is ``scripts/ux_runner/session.py``'s, so
   the first-time-user stories hold their videos to the same function). That is
   decided as soon as the page is signed in, before anything is recorded
   (``_Run._video_withheld``), and applied before anything is rendered
   (``_Run._video``). Against any other stack nothing is
   rendered, so no mp4 reaches the note or the job's artifacts, and the section
   says the video was withheld. :data:`DEV_VIDEO_FLAG` renders one all the same,
   for a developer's local dry run only: it forces ``--dry-run``, :func:`main`
   refuses it wherever ``CI`` or ``GITLAB_CI`` is set, and neither CI job carries
   it.

What it does NOT protect, stated rather than implied
----------------------------------------------------
* **The trust root is the MR's own pipeline definition.** An MR pipeline uses the
  MR's ``.gitlab-ci.yml``, so an author with push access can rewrite this job
  itself; pinning the harness protects against edits to the harness files and
  nothing more. ``UX_TEST_PERSONAS`` is scoped to the environment ``ux-review``,
  so it reaches only a job that declares that environment, and this repository's
  pipeline definition limits that to this one. ``UX_TEST_UI_ENV`` is stored at
  ``ux-review-build`` as well, the environment of ``ux_test_build``, which runs
  the MR's ``npm ci`` and needs the UI environment alone. An MR's own copy of the
  definition, which is what its pipeline runs, can declare either environment on
  any job. ``GITLAB_REVIEW_TOKEN``, shared with ``ai_mr_review``, keeps the
  default scope and reaches every job. The bound is the one ``ai_mr_review``
  relies on: a fork's pipeline gets none of these variables unless a member of the
  project runs it there, so this needs push access.
* **The MR's UI code holds the persona's credentials while it runs.** It is the
  login form, so it receives the password and the session's tokens, and it can
  reach the shared regional Cognito endpoints, through which it could hand them to
  a user pool its author controls. That gives an author nothing new, since an MR
  author can read ``UX_TEST_PERSONAS`` anyway; signing the session out globally
  after each run is the later hardening.
* **The video's frames are uploaded unredacted.** The narration, the captions and
  every text artifact are redacted, but the frames are pictures of the screen, and
  whatever the MR's UI drew on the review stack, which holds only shipped example
  data, is in them as it was shown; property 6 is what keeps a video to that
  stack. The long-lived persona carries a display name
  (``scripts/ux_test_session.py --long-lived``), so the top bar shows that rather
  than its sign-in address once the page has loaded it; until then, on every full
  page load, and on a page that lists users, the frames show the address, which
  is a fixed, non-secret name. The password never reaches a frame.
* **The review stack is known by its name, not by its account or its region.**
  Nothing in ``UX_TEST_UI_ENV`` names an account, and ``UX_TEST_AWS_ACCOUNT_ID``
  pins the job's credentials rather than the stack's endpoints. The environment
  does name a region, but nothing records which region the review stack is in
  (the nightly deploys it to its own pipeline's), and its settings parameter has
  the same name in every region. So a stack called ``idp-ux-nightly`` in another
  account, or in another region of the same one, with its own UI environment,
  satisfies property 6. And the stack the page reports is the MR's own UI's to
  render, so that answer can withhold a video but never stands in for the other
  two.
* **The version and build in the note's heading and backend caveat are the MR's to
  choose.** The MR's own UI renders them, so they are shown only in the expected
  shapes and labelled as the UI reports them.
* **The socket audit is an alarm, not the enforcement.** It polls, so a socket
  opened and closed between two polls is not seen, during the sign-in as at any
  other time; the proxy is the enforcement. It needs the job to run as root;
  without that it is unavailable, and then nothing is posted.
* **The proxy confines Chrome as configured, not a subverted one.** Chrome runs
  with ``--no-sandbox``, so a renderer the MR's page compromises runs as
  ``uxbrowser`` with sockets of its own to anything the job container can reach.
  The proxy never sees that traffic and the audit only samples it. The pinned
  Chrome for Testing build also gets no security fix until the pin is moved.
* **Loopback is only as private as the job's network namespace.** DevTools on
  ``127.0.0.1:9222`` and the proxy on ``127.0.0.1:3128`` are unreachable from
  outside the job because Docker's default network gives each job a loopback of
  its own. A runner that runs jobs with host networking shares that loopback
  with every job on the host.
* **Root's children share root's ``/proc``.** ``claude`` and both MCP servers run
  as this process's uid, so they could read its ``/proc/<pid>/environ``, which
  holds ``GITLAB_REVIEW_TOKEN``, ``UX_TEST_PERSONAS``, ``UX_TEST_UI_ENV`` and the
  job's AWS keys; what keeps those from the model is that it holds no tool that
  reads a file (``agent``). The same holds for the job's apt and toolchain
  install, which inherit none of those but run as root beside the job's shell.
* **The review stack is shared.** The contract forbids every write, but a model
  that disobeys could still change shared fixtures.

Usage
-----
    # CI: the precheck (a bare python3, before the toolchain is installed) ...
    python3 -B "$UX_HARNESS_DIR/$UX_ENTRY" $UX_MODE_ARGS --precheck \\
        --artifact-dir "$UX_ARTIFACTS"
    # ... then, only when it wrote proceed in the artifact directory, the full run
    python3 -B "$UX_HARNESS_DIR/$UX_ENTRY" $UX_MODE_ARGS \\
        --inputs "$UX_ARTIFACTS/inputs.json" --artifact-dir "$UX_ARTIFACTS"

    # locally, in Docker, as a dry run (section.md and the mp4 land in scratch/)
    make ai-ux-test-local MR=812

    # against a stack of your own, whose video is rendered only with the opt-in
    make ai-ux-test-local MR=812 STACK=<stack> EXTRA_ARGS=--dev-video-any-stack

    # what run_in_docker.py runs on the host: collect the MR from git only
    python3 scripts/sdlc/ai_ux_test.py --mr 812 --collect-only --inputs /tmp/inputs.json

Environment
-----------
In CI the three ``UX_TEST_*`` variables are masked, unprotected and scoped to the
environment ``ux-review``, which this job alone declares, with ``action: prepare``.
``ux_test_build`` declares ``ux-review-build``, at which a second copy of
``UX_TEST_UI_ENV`` alone is stored, so the job that builds the MR's UI never holds
the persona's password. A variable left at the default scope reaches every job of
every pipeline.

``GITLAB_REVIEW_TOKEN``
    The project access token ``ai_mr_review`` posts with, shared with it. Needed to
    read the MR and its notes and to post; ``--dry-run`` runs without it.
``UX_TEST_UI_ENV``
    Base64 of the review stack's ``WebUITestEnvFile`` output, plus an optional
    ``UX_TEST_BUCKETS=`` line of the stack's exact bucket names. The egress rules
    are derived from it, and its settings parameter is one of the three
    statements of the stack's identity (property 6).
``UX_TEST_PERSONAS``
    Base64 of ``{"<group>": {"username": ..., "password": ...}}``.
``UX_TEST_AWS_ACCOUNT_ID``
    The account the job's credentials must reach. Shown only by its last four digits.
``CI_API_V4_URL``, ``CI_PROJECT_ID``
    Supplied by GitLab CI. Outside CI, pass ``--api-url`` / ``--project``; there is
    no built-in API URL, and without one the GitLab API is not used at all, so
    ``--collect-only`` takes the title from git and every other mode is skipped
    whenever ``GITLAB_REVIEW_TOKEN`` is set. The instance's host, from this URL and
    ``CI_SERVER_HOST``, is redacted wherever it appears.
``ANTHROPIC_MODEL``, ``AWS_REGION`` / ``AWS_DEFAULT_REGION``
    The Bedrock model id and the region of Bedrock, STS and Polly.
``UX_HARNESS``, ``UX_ENTRY``
    The paths the CI job extracted from the target branch, and the one of them it
    runs, as the job's variables give them: ``scripts/sdlc/ai_ux_test.py`` with
    ``--mr "$CI_MERGE_REQUEST_IID"`` in ``UX_MODE_ARGS`` and ``ux-tests`` in
    ``UX_ARTIFACTS``. ``--precheck`` fails unless they are exactly
    :data:`HARNESS_PATHS`, in order, and :data:`ENTRY`, because a project or
    pipeline variable overrides a job variable and the pin must not depend on it.
    The one exception is a job that sets neither, both absent rather than empty:
    one defined before these variables, which names its harness and this file in
    its own script where no variable reaches them, and which a merge request whose
    ``.gitlab-ci.yml`` predates them still runs. Its precheck goes on, as a
    variable cannot have changed what it extracts or runs.
``CI_JOB_STARTED_AT``
    When the job started, so ``--deadline-minutes`` counts the install as well.
``CI_COMMIT_SHA``, ``CI_MERGE_REQUEST_SOURCE_BRANCH_SHA``
    The head this pipeline, and so its UI build, is of. A newer head means another
    pipeline owns the test, and this one stands down.
``CI``, ``GITLAB_CI``
    Set by GitLab CI in every job. Where either is set, :data:`DEV_VIDEO_FLAG` is
    refused.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import ux_test_proxy  # noqa: E402
from ux_runner import (  # noqa: E402
    account,
    agent,
    artifacts,
    chrome,
    egress,
    flows,
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
    ui_change,
)

review = gitlab_api.review

#: Every path the CI job extracts from the target branch with ``git archive`` and
#: runs from: this file, its imports, and what the prompt inlines. A test closes it
#: over the imports of every module it names, and the job's own list in
#: ``.gitlab-ci.yml`` must equal it, so a new harness module cannot be imported
#: from the MR's checkout by accident.
HARNESS_PATHS = (
    "scripts/sdlc/ai_ux_test.py",
    "scripts/sdlc/ai_mr_review.py",
    "scripts/ux_runner",
    "scripts/ux_test_proxy.py",
    "scripts/ux_recorder.py",
    "scripts/ux_recorder_cdp.py",
    "scripts/ux_recorder_render.py",
    "scripts/ux_recorder_mcp.py",
    "scripts/check_markdown_links.py",
    "scripts/ux_flows.yaml",
    ".claude/skills/ux-test.md",
    ".claude/skills/ux-test-ci.md",
)
#: This file, as the CI job's ``UX_ENTRY`` must name it, relative to the tree.
ENTRY = "scripts/sdlc/ai_ux_test.py"
#: The two skills the prompt inlines, in that order. Read from the harness, so the
#: MR under test cannot rewrite its own instructions.
SKILL_FILES = (".claude/skills/ux-test-ci.md", ".claude/skills/ux-test.md")
#: The flow catalogue the selector reads, from the harness for the same reason.
FLOWS_FILE = "scripts/ux_flows.yaml"
#: The long-lived review stack, and the one stack whose screens this job turns into
#: a video, because it holds only shipped example data and the frames are not
#: redacted (property 6). ``--stack`` defaults to it. It is ``session.REVIEW_STACK``
#: itself, which a test pins equal to ``codebuild_deployment.UX_NIGHTLY_STACK_NAME``,
#: the stack the nightly updates.
REVIEW_STACK = session.REVIEW_STACK
#: A developer's opt-in to a video of another stack, in a local dry run. It forces
#: ``--dry-run``, :func:`main` refuses it under CI, and a test pins that neither CI
#: job carries it.
DEV_VIDEO_FLAG = "--dev-video-any-stack"
#: Set by GitLab CI in every job; wherever either is set, :func:`main` refuses
#: :data:`DEV_VIDEO_FLAG`. A test pins ``run_in_docker.py`` to forwarding exactly
#: these into its review container, so its runs are refused on a CI host too.
CI_VARIABLES = ("CI", "GITLAB_CI")
DEFAULT_PERSONA = "Admin"

_DEFAULT_REGION = "us-east-1"
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_ACCOUNT_ID_RE = re.compile(r"[0-9]{12}")
#: Time the run needs after the session ends: stop, render, upload and post.
_RESERVE_MINUTES = 12
#: The least time render is given, however little of the deadline is left.
_RENDER_MIN_SECONDS = 300.0
#: The fields of a recorded event that hold text from the model or the page: a
#: mark's chapter label and spoken line, a note, and a click's three labels.
_EVENT_TEXT_FIELDS = ("label", "say", "text", "target", "under", "interactive")
#: What the recorder writes at stop from the events' spoken lines, and what render
#: reads in preference to them.
_NARRATION_OVERRIDE = "narration.md"
#: Seconds between two looks for the code-review note.
_POLL_SECONDS = gitlab_api.POLL_SECONDS
#: How many older notes of this job's a post marks superseded, newest first.
_SUPERSEDE_LIMIT = 10
_STDERR_CHARS = 400
_MIB = 1024 * 1024
_RECORDER_CURRENT = "current.json"
_RECORDER_STOP_SAY = "That ends the UX test."
_NO_VISIBLE_CHANGE = "_No user-visible change found, so no video is attached._"
_NO_MARKS = "_The session marked no chapters, so no video is attached._"
_VIDEO_WITHHELD = (
    "_The video was withheld, because {reason}: recording another stack's screens "
    "needs a privacy decision of its own._"
)
#: ``summary.recording.narration`` when narration was asked for and no Polly-only
#: session could be minted, so render ran with no credential at all.
_NARRATION_UNSCOPED = "unavailable (no Polly session)"
_OVER_LIMIT = (
    "_The video ({size} MB) is over the upload limit; it is in the job artifacts._"
)
_UPLOAD_FAILED = "_The video could not be uploaded; it is in the job artifacts._"
#: The inputs.json fields that are MR-written prose, redacted when the record is
#: written. The SHAs, the target branch and the changed paths stay exact, because
#: the full run classifies and labels from them.
_INPUTS_PROSE_FIELDS = ("title", "author", "web_url", "source_branch", "ui_diff")
_TOOLCHAIN_KEYS = ("chrome", "node", "chrome_devtools_mcp", "claude_code")
_EGRESS_KEYS = ("refused", "served", "connect_allowed", "upstream_failed", "by_reason")


@dataclass
class Runtime:
    """Every collaborator that touches a process, the network, AWS or the clock.

    Each field defaults (in :func:`default_runtime`) to the module function of the
    same job and takes its signature, so the tests drive :func:`run` end to end with
    fakes and no subprocess, network or AWS call.
    """

    forge: Callable[[argparse.Namespace, str], gitlab_api.UxGitlab]
    collect: Callable[..., mr_inputs.MrInputs]
    sts: Callable[[str], Any]
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
    start_recorder: Callable[..., dict[str, Any]]
    stop_recorder: Callable[..., dict[str, Any]]
    kill_recorder: Callable[..., None]
    recorder_status: Callable[[Path], dict[str, Any] | None]
    run_agent: Callable[..., tuple[agent.AgentResult | None, processes.Completed]]
    render_video: Callable[..., tuple[Path | None, str]]
    chapters: Callable[..., list[tuple[str, str]]]
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    environ: dict[str, str] = field(default_factory=lambda: dict(os.environ))


@dataclass
class Decision:
    """What the precheck concluded. ``status`` is proceed, skipped or unchanged."""

    proceed: bool
    status: str
    detail: str
    inputs: mr_inputs.MrInputs | None
    change: ui_change.UiChange | None


def _default_forge(args: argparse.Namespace, credential: str) -> gitlab_api.UxGitlab:
    """The real GitLab client for ``--api-url`` and ``--project``."""
    return gitlab_api.UxGitlab(args.api_url, args.project, credential)


def default_runtime() -> Runtime:
    """The runtime the job uses: every collaborator is the real one."""
    return Runtime(
        forge=_default_forge,
        collect=mr_inputs.collect,
        sts=account.sts_client,
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
        start_recorder=recording.start,
        stop_recorder=recording.stop,
        kill_recorder=recording.kill,
        recorder_status=recording.status,
        run_agent=agent.run_agent,
        render_video=recording.render_video,
        chapters=recording.chapters,
    )


def _scrubbed_report(text: str, scrub: artifacts.Scrub) -> str:
    """The model's final message, redacted, with its report block redacted by value.

    ``section.parse_report`` sanitises every field, and sanitising changes how a
    value is spelled (a zero-width space after ``@``, or before a digit after
    ``!``, control characters turned into spaces, and a cut at the field's bound),
    as does JSON's own ``\\uXXXX`` escaping. A secret quoted in a field in any of
    those spellings would no longer match the redactor once the section is
    composed. So the block ``parse_report`` reads (the last fenced ``json`` block,
    found by its own two patterns) is decoded, its strings are redacted as decoded
    values, each flattened first as ``sanitize`` will flatten it
    (:meth:`artifacts.Scrub.field`), and it is written back in place. Both ``report.md``
    and ``parse_report`` read the result. A block that does not decode is left as
    redacted text, for ``parse_report`` to refuse.
    """
    redacted = scrub(text)
    openers = list(section._FENCE_OPEN_RE.finditer(redacted))
    if not openers:
        return redacted
    start = openers[-1].end()
    closer = section._FENCE_CLOSE_RE.search(redacted, start)
    if closer is None:
        return redacted
    try:
        data = json.loads(redacted[start : closer.start()])
    except (ValueError, RecursionError):
        return redacted
    clean = json.dumps(scrub.tree(data, fields=True), indent=2, ensure_ascii=False)
    return f"{redacted[:start]}\n{clean}\n{redacted[closer.start() :]}"


def _job_elapsed(environ: Mapping[str, str]) -> float:
    """Seconds since the CI job started (``CI_JOB_STARTED_AT``); 0 outside CI."""
    started = environ.get("CI_JOB_STARTED_AT", "").strip()
    if not started:
        return 0.0
    try:
        when = datetime.fromisoformat(started.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - when).total_seconds())


def _pipeline_head(environ: Mapping[str, str]) -> str | None:
    """The MR head this pipeline, and so its UI build, is of; None outside CI.

    A merged-results pipeline builds a merge commit (``CI_COMMIT_SHA``) and names
    the source head in ``CI_MERGE_REQUEST_SOURCE_BRANCH_SHA``; an ordinary MR
    pipeline leaves that empty and builds the source head itself.
    """
    for name in ("CI_MERGE_REQUEST_SOURCE_BRANCH_SHA", "CI_COMMIT_SHA"):
        value = environ.get(name, "").strip()
        if value:
            return value if _SHA_RE.fullmatch(value) else None
    return None


def _body(note: Mapping[str, Any]) -> str:
    body = note.get("body")
    return body if isinstance(body, str) else ""


def _no_token_message() -> str:
    """Why nothing can be checked or posted, naming the variable and the remedy."""
    return (
        f"{review.GITLAB.credential_env} is not set, so the UX test can be neither "
        "checked against the MR's notes nor posted. It is the masked, unprotected "
        "project access token ai_mr_review posts with (`api` scope); pass "
        "--dry-run to run without posting."
    )


def _forge(
    args: argparse.Namespace, runtime: Runtime, credential: str
) -> gitlab_api.UxGitlab:
    """The GitLab client, or a skip naming whichever of its two settings is missing.

    The API URL has no built-in default: GitLab CI always sets ``CI_API_V4_URL``,
    and anywhere else the instance is the caller's to name, so a missing one takes
    the same path as an API that cannot be reached.
    """
    if not args.api_url:
        raise review.Skip(
            "no GitLab API URL: pass --api-url or set CI_API_V4_URL, as GitLab CI does"
        )
    if not args.project:
        raise review.Skip("no project: pass --project or run inside GitLab CI")
    return runtime.forge(args, credential)


def _with_metadata(
    inputs: mr_inputs.MrInputs, merge_request: review.MergeRequest
) -> mr_inputs.MrInputs:
    """``inputs`` with the API's title, author, URL and source branch, when given."""
    title = merge_request.title[: mr_inputs.TITLE_CHARS]
    return dataclasses.replace(
        inputs,
        title=title or inputs.title,
        author=merge_request.author or inputs.author,
        web_url=merge_request.web_url or inputs.web_url,
        source_branch=merge_request.source_branch or inputs.source_branch,
        title_from_git=inputs.title_from_git and not title,
    )


def _inputs_for(args: argparse.Namespace, runtime: Runtime) -> mr_inputs.MrInputs:
    """The MR's inputs, read from ``--inputs`` or collected from git.

    A record for another MR than ``--mr`` is refused: it would label one MR's
    findings with another's head.
    """
    try:
        if args.inputs is not None:
            inputs = mr_inputs.read(Path(args.inputs))
        else:
            inputs = runtime.collect(
                Path(args.repo_dir), args.mr, args.target_branch, remote=args.remote
            )
    except mr_inputs.InputsError as error:
        raise review.Failure(f"the MR's inputs could not be read: {error}") from None
    if inputs.iid != args.mr:
        raise review.Failure(f"the inputs describe MR !{inputs.iid}, not !{args.mr}")
    return inputs


def _settings_problem(args: argparse.Namespace, environ: Mapping[str, str]) -> str:
    """Why the CI variables cannot drive a run, or ``""`` when they can.

    Each value is decoded and checked here, in the precheck, so a misconfigured
    variable costs a minute rather than a toolchain install. No message carries a
    value.
    """
    ui_text = environ.get("UX_TEST_UI_ENV", "")
    if not ui_text.strip():
        return (
            "UX_TEST_UI_ENV is not set, so the browser's egress rules cannot be "
            "derived from the review stack's UI environment"
        )
    try:
        ux_test_proxy.stack_rules(egress.ui_env_from_b64(ui_text), args.stack)
    except ux_test_proxy.EgressConfigError as error:
        return f"UX_TEST_UI_ENV cannot be used: {error}"
    persona_text = environ.get("UX_TEST_PERSONAS", "")
    if not persona_text.strip():
        return "UX_TEST_PERSONAS is not set, so no persona can be signed in"
    try:
        personas = signin.personas_from_env(persona_text)
    except signin.SignInError as error:
        return str(error)
    if args.persona not in personas:
        return f"UX_TEST_PERSONAS has no {args.persona!r} persona"
    expected = environ.get("UX_TEST_AWS_ACCOUNT_ID", "").strip()
    if not expected:
        return (
            "UX_TEST_AWS_ACCOUNT_ID is not set, so the job cannot check which "
            "account its credentials reach"
        )
    if not _ACCOUNT_ID_RE.fullmatch(expected):
        return "UX_TEST_AWS_ACCOUNT_ID is not a 12-digit account id"
    return ""


#: Why the run's stack is not established as :data:`REVIEW_STACK`, or ``""``: the
#: one check of which stack a run used, ``session.review_stack_problem`` itself, so
#: the MR UX test and the first-time-user stories decide a video by one function.
review_stack_problem = session.review_stack_problem


def precheck(args: argparse.Namespace, runtime: Runtime) -> Decision:
    """Whether this head is worth a browser, decided from git and the API alone.

    It runs on a bare python3 before the toolchain is installed, so a draft, a
    test-only change, a head that is already tested or a missing precondition stops
    the job in about a minute. In order: the inputs; with a token, the MR itself
    (drafts are not tested, a head that has moved on is left to the newer head's
    pipeline, and the title, author and URL come from it); the change, classified
    again here from git because ``ui-change.json`` was written by a job that ran the
    MR's code; the UI build; the head the pipeline built; the CI variables; the
    token; and, with a token, an existing section of this job's for the head.
    ``review.Failure`` means git or a file could not answer.

    The two head checks exist because the build and the inputs are taken at
    different moments. Testing an older build under a newer head's name would post
    a section that describes code the head no longer has, and its marker would then
    stop the newer head's own run as already tested.
    """
    environ = runtime.environ
    credential = environ.get(review.GITLAB.credential_env, "").strip()
    inputs = _inputs_for(args, runtime)
    forge: gitlab_api.UxGitlab | None = None
    if credential:
        try:
            forge = _forge(args, runtime, credential)
            merge_request = forge.merge_request(args.mr)
        except review.Skip as skip:
            return Decision(False, "skipped", str(skip), inputs, None)
        if merge_request.draft or merge_request.title.startswith("Draft:"):
            return Decision(
                False, "skipped", "a draft; drafts are not tested", inputs, None
            )
        moved = merge_request.head_sha
        if moved and moved != inputs.head_sha:
            return Decision(
                False,
                "skipped",
                f"the MR head moved to {moved[:8]} after {inputs.head_sha[:8]} was "
                "collected; a pipeline for the new head tests it",
                inputs,
                None,
            )
        inputs = _with_metadata(inputs, merge_request)
    change = ui_change.classify(inputs.changed)
    if change.skip:
        return Decision(False, "skipped", change.reason, inputs, change)
    if not (Path(args.build_dir) / "index.html").is_file():
        return Decision(
            False,
            "skipped",
            "no UI build: ux_test_build was skipped or failed",
            inputs,
            change,
        )
    built = _pipeline_head(environ)
    if built is not None and built != inputs.head_sha:
        return Decision(
            False,
            "skipped",
            f"this pipeline built {built[:8]}, but the MR head is now "
            f"{inputs.head_sha[:8]}; the newer head's own pipeline tests it",
            inputs,
            change,
        )
    problem = _settings_problem(args, environ)
    if problem:
        return Decision(False, "skipped", problem, inputs, change)
    if forge is None:
        if not args.dry_run:
            return Decision(False, "skipped", _no_token_message(), inputs, change)
    else:
        try:
            me = forge.current_user()["id"]
            mine = gitlab_api.my_notes(forge.notes(args.mr), me)
        except review.Skip as skip:
            return Decision(False, "skipped", str(skip), inputs, change)
        if any(section.has_section(_body(note), inputs.head_sha) for note in mine):
            return Decision(False, "unchanged", "already tested", inputs, change)
    count = len(change.user_visible)
    return Decision(True, "proceed", f"{count} user-visible file(s)", inputs, change)


def _write_inputs(
    inputs: mr_inputs.MrInputs, path: Path, scrub: artifacts.Scrub
) -> Path:
    """Write ``inputs.json`` with its prose fields redacted and the rest exact."""
    record = inputs.to_json()
    for name in _INPUTS_PROSE_FIELDS:
        record[name] = scrub(record[name])
    path.parent.mkdir(parents=True, exist_ok=True)
    artifacts.write_artifact(
        path, json.dumps(record, indent=2, ensure_ascii=False) + "\n"
    )
    return path


def _new_summary(args: argparse.Namespace) -> dict[str, Any]:
    """The skeleton of ``summary.json`` (D-SUMMARY), filled in as the run goes."""
    return {
        "version": 1,
        "mr": args.mr,
        "head_sha": "",
        "target_branch": args.target_branch,
        "status": "failed",
        "detail": "",
        "stack": {
            "name": args.stack,
            "region": None,
            "version": None,
            "build": None,
            "facts_source": "ui",
            "account_check": "not_run",
            "review_stack": None,
        },
        "flows": [],
        "model": {
            "id": args.model,
            "cost_usd": None,
            "num_turns": None,
            "duration_s": None,
            "stop": None,
        },
        "limits": {
            "max_turns": args.max_turns,
            "max_budget_usd": args.max_budget_usd,
            "session_minutes": args.session_minutes,
        },
        "recording": {
            "marks": 0,
            "clicks_rejected": 0,
            "cdp_messages_dropped": 0,
            "narration": None,
            "video_bytes": None,
            "video_attached": False,
            "video_withheld": None,
            "dev_video_any_stack": args.dev_video_any_stack,
        },
        "egress": {},
        "socket_audit": {
            "state": "not_run",
            "violations": [],
            "sockets": 0,
            "strays": [],
        },
        "credential_scope": {"claude": None, "render": None},
        "confinement": "dev-unconfined" if args.dev_unconfined else "full",
        "browser_uid_switched": False,
        "viewport": None,
        "timings_s": {},
        "note": {
            "action": "none",
            "note_id": None,
            "upload_id": None,
            "upload_error": None,
            "superseded": [],
        },
        "toolchain": {},
    }


def _exit_code(status: str, args: argparse.Namespace) -> int:
    if status == "failed":
        return 1
    return 1 if status == "skipped" and args.fail_on_skip else 0


def _say(scrub: artifacts.Scrub, text: str) -> None:
    print(scrub(text), flush=True)


def _unexpected(scrub: artifacts.Scrub, error: Exception) -> str:
    """Print a redacted traceback to stderr, and return the one-line reason.

    Python's own handler would print the traceback unredacted, and an exception's
    text can carry whatever the call that raised it was handling.
    """
    trace = "".join(traceback.format_exception(error))
    print(scrub(trace), file=sys.stderr, flush=True)
    return f"{type(error).__name__}: {error}"


def _precheck_main(args: argparse.Namespace, runtime: Runtime) -> int:
    """``--precheck``: decide, write the decision files, print one decision line.

    ``inputs.json`` is what the full run reads back, and ``proceed`` is the empty
    marker the job's script tests; a stale marker is removed first, so only this
    decision can leave one. The first line printed is ``PROCEED:``, ``SKIPPED:`` or
    ``UNCHANGED:``. When the decision is final, ``summary.json`` is written too. An
    artifact directory that is a symlink fails the precheck before anything is
    removed from it or written to it (``artifacts.artifact_dir``).

    Before any of that, the job's ``UX_HARNESS`` and ``UX_ENTRY`` must name exactly
    :data:`HARNESS_PATHS` and :data:`ENTRY` (``paths.harness_pin_problem``): the job
    extracts and runs what those variables say, and a project or pipeline variable
    overrides the job's own, so anything else fails here, before the artifact
    directory is touched. A job that sets neither is one defined before them, which
    names its harness and this file in its own script, so it runs: a merge request
    whose ``.gitlab-ci.yml`` is older than its target branch's harness still has it.
    """
    scrub = artifacts.Scrub(artifacts.redactor_for(runtime.environ))
    problem = paths.harness_pin_problem(
        runtime.environ, harness_paths=HARNESS_PATHS, entry=ENTRY, script_named=True
    )
    if problem:
        _say(scrub, f"FAILED: {problem}")
        return 1
    began = runtime.clock()
    summary = _new_summary(args)
    try:
        out = artifacts.artifact_dir(Path(args.artifact_dir))
    except review.Failure as refused:
        _say(scrub, f"FAILED: {refused}")
        return 1
    try:
        (out / "proceed").unlink(missing_ok=True)
        decision = precheck(args, runtime)
        elapsed = runtime.clock() - began
        return _record_decision(args, decision, summary, scrub, elapsed)
    except Exception as error:
        with contextlib.suppress(OSError):
            (out / "proceed").unlink(missing_ok=True)
        reason = str(error) if isinstance(error, review.Failure) else ""
        summary["detail"] = reason or _unexpected(scrub, error)
        _say(scrub, f"FAILED: {summary['detail']}")
        with contextlib.suppress(OSError, review.Failure):
            artifacts.write_json(out / "summary.json", summary, scrub)
        return 1


def _record_decision(
    args: argparse.Namespace,
    decision: Decision,
    summary: dict[str, Any],
    scrub: artifacts.Scrub,
    elapsed: float,
) -> int:
    """Write the precheck's files and print its one decision line."""
    out = Path(args.artifact_dir)
    head = decision.inputs.head_sha if decision.inputs is not None else ""
    if decision.inputs is not None:
        scrub.keep(decision.inputs.head_sha, decision.inputs.base_sha)
        _write_inputs(decision.inputs, out / "inputs.json", scrub)
    artifacts.write_json(
        out / "precheck.json",
        {
            "version": 1,
            "proceed": decision.proceed,
            "status": decision.status,
            "detail": decision.detail,
            "head_sha": head,
        },
        scrub,
    )
    if decision.proceed:
        artifacts.write_artifact(out / "proceed", "")
        _say(scrub, f"PROCEED: {decision.detail} at {head[:8]}")
        return 0
    _say(scrub, f"{decision.status.upper()}: {decision.detail}")
    summary.update(head_sha=head, status=decision.status, detail=decision.detail)
    summary["timings_s"] = {"precheck": round(elapsed, 1)}
    artifacts.write_json(out / "summary.json", summary, scrub)
    return _exit_code(decision.status, args)


def _collect_main(args: argparse.Namespace, runtime: Runtime) -> int:
    """``--collect-only``: write the MR's inputs for a run elsewhere, from git.

    The Docker review mode runs this on the host, where git and SSH work, and hands
    the file to a container that mounts the repository read-only. API metadata is
    used when a token is set; an API that cannot be reached from the host (a
    federated sign-in gateway) only costs the title, which then comes from the head
    commit.
    """
    scrub = artifacts.Scrub(artifacts.redactor_for(runtime.environ))
    target = (
        Path(args.inputs) if args.inputs else Path(args.artifact_dir) / "inputs.json"
    )
    credential = runtime.environ.get(review.GITLAB.credential_env, "").strip()
    merge_request: review.MergeRequest | None = None
    try:
        if credential:
            try:
                merge_request = _forge(args, runtime, credential).merge_request(args.mr)
            except review.Skip as skip:
                _say(scrub, f"no API metadata ({skip}); the title is the head commit's")
        inputs = runtime.collect(
            Path(args.repo_dir), args.mr, args.target_branch, remote=args.remote
        )
        if merge_request is not None:
            inputs = _with_metadata(inputs, merge_request)
        scrub.keep(inputs.head_sha, inputs.base_sha)
        _write_inputs(inputs, target, scrub)
    except (mr_inputs.InputsError, review.Failure) as error:
        _say(scrub, f"FAILED: the MR could not be collected: {error}")
        return 1
    except Exception as error:
        _say(scrub, f"FAILED: {_unexpected(scrub, error)}")
        return 1
    _say(scrub, f"collected: {target}")
    return 0


@dataclass
class _Video:
    """What the note can carry: the artifact mp4 when attachable, else a sentence.

    ``chapters`` are the attachable video's, read back with it (``_Run._video``).
    """

    file: Path | None
    attachable: bool
    note: str | None
    chapters: list[tuple[str, str]] = field(default_factory=list)


def _search_path(tool: str = "node", base: str = agent.DEFAULT_SEARCH_PATH) -> str:
    """``PATH`` for a child: the directory ``tool`` is found in here, then ``base``.

    The MCP servers need ``node`` and render needs ffmpeg, and on a development
    host either can live outside the fixed ``base`` (Homebrew's ``/opt/homebrew``).
    """
    found = shutil.which(tool)
    if not found:
        return base
    tool_dir = str(Path(found).parent)
    if tool_dir in base.split(os.pathsep):
        return base
    return f"{tool_dir}{os.pathsep}{base}"


def _skills() -> dict[str, str]:
    """Both pinned skills, by file name, read from the harness tree."""
    try:
        return {
            Path(name).name: (paths.HARNESS_ROOT / name).read_text(encoding="utf-8")
            for name in SKILL_FILES
        }
    except OSError as error:
        raise review.Failure(
            f"the pinned skill files cannot be read: {error}"
        ) from None


def _marks_on_disk(session_dir: Path) -> int:
    """Marks counted from the finalised ``timeline.json``, for a silent recorder."""
    try:
        timeline = json.loads((session_dir / "timeline.json").read_text("utf-8"))
    except (OSError, ValueError):
        return 0
    events = timeline.get("events") if isinstance(timeline, dict) else None
    if not isinstance(events, list):
        return 0
    return sum(1 for e in events if isinstance(e, dict) and e.get("type") == "mark")


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


#: Exceptions that mean a step of the run could not be done, reported as FAILED
#: with their own message, which names the step.
_STEP_FAILURES: tuple[type[Exception], ...] = (
    review.Failure,
    egress.ProxyError,
    chrome.ChromeError,
    signin.SignInError,
    recording.RecorderError,
    agent.AgentError,
    flows.FlowsError,
    ux_test_proxy.EgressConfigError,
    section.ReportError,
    session.SessionError,
)


class _Run:
    """One full run: the state the cleanup needs, and the summary it fills in.

    The run's private directory is made under ``/tmp`` rather than ``$TMPDIR``,
    because Chrome and the proxy run as two other users who must be able to
    traverse into it, and a per-user ``$TMPDIR`` (macOS) is closed to them. It is
    ``0o711``: traversable, not listable, and each tool's own subdirectory is
    private to its user. The browser itself, the proxy and the socket watch are
    :attr:`browser`'s (``session.ConfinedBrowser``).
    """

    def __init__(self, args: argparse.Namespace, runtime: Runtime) -> None:
        self.args = args
        self.runtime = runtime
        self.scrub = artifacts.Scrub(artifacts.redactor_for(runtime.environ))
        self.started = runtime.clock()
        self.deadline = (
            self.started + args.deadline_minutes * 60 - _job_elapsed(runtime.environ)
        )
        self.artifacts = Path(args.artifact_dir)
        self.summary = _new_summary(args)
        self.timings: dict[str, float] = {}
        self.cancelled = False
        self.inputs: mr_inputs.MrInputs | None = None
        self.run_dir: Path | None = None
        self.browser = session.ConfinedBrowser(runtime, say=self._say)
        self.audit: chrome.SocketAudit | None = None
        self.rec_out: Path | None = None

    def execute(self) -> int:
        """Run, clean up, and write ``summary.json`` whatever happened.

        Except into an artifact directory that is a symlink: that is refused
        before anything starts, and nothing at all is written through it.
        """
        try:
            artifacts.artifact_dir(self.artifacts)
        except review.Failure as refused:
            self._say(f"FAILED: {refused}")
            return 1
        code = self._guarded()
        self.timings["total"] = self.runtime.clock() - self.started
        self.summary["timings_s"] = {k: round(v, 1) for k, v in self.timings.items()}
        try:
            artifacts.write_json(
                self.artifacts / "summary.json", self.summary, self.scrub
            )
        except review.Failure as refused:
            self._say(f"FAILED: {refused}")
            return 1
        except OSError as error:
            self._say(f"FAILED: summary.json could not be written: {error}")
            return 1
        return code

    def _guarded(self) -> int:
        """:meth:`_run` inside the cleanup, with every outcome mapped to a status."""
        try:
            try:
                code = self._run()
            finally:
                self._cleanup()
        except (processes.Terminated, KeyboardInterrupt):
            return self._finish("failed", "cancelled")
        except review.Skip as skip:
            return self._finish("skipped", str(skip))
        except _STEP_FAILURES as failure:
            return self._finish("failed", str(failure))
        except Exception as error:
            return self._finish("failed", _unexpected(self.scrub, error))
        if self.cancelled:
            return self._finish("failed", "cancelled")
        return code

    def _say(self, text: str) -> None:
        _say(self.scrub, text)

    def _finish(self, status: str, detail: str) -> int:
        self.summary["status"] = status
        self.summary["detail"] = detail
        self._say(f"{status.upper().replace('_', ' ')}: {detail}")
        return _exit_code(status, self.args)

    def _write(self, name: str, text: str) -> Path:
        path = self.artifacts / name
        artifacts.write_artifact(path, self.scrub(text))
        return path

    def _run(self) -> int:
        args, rt = self.args, self.runtime
        began = rt.clock()
        decision = precheck(args, rt)
        self.timings["precheck"] = rt.clock() - began
        if decision.inputs is not None:
            self.inputs = decision.inputs
            self.scrub.keep(decision.inputs.head_sha, decision.inputs.base_sha)
            self.summary["head_sha"] = decision.inputs.head_sha
        if not decision.proceed:
            return self._finish(decision.status, decision.detail)
        inputs, change = decision.inputs, decision.change
        if inputs is None or change is None:
            raise review.Failure("the precheck proceeded without inputs")
        self._say(f"PROCEED: {decision.detail} at {inputs.head_sha[:8]}")
        return self._confined_run(inputs, change)

    def _confined_run(
        self, inputs: mr_inputs.MrInputs, change: ui_change.UiChange
    ) -> int:
        """Everything after the precheck proceeded, in the order the job needs it.

        Each phase leaves on ``self`` what the cleanup must stop, so a failure or a
        cancel at any point stops exactly what had been started.
        """
        args, rt = self.args, self.runtime
        setup_began = rt.clock()
        tc = self._toolchain()
        sts, identity = self._account()
        chosen, flow_ids = self._flows(change)
        ui_env = egress.ui_env_from_b64(rt.environ.get("UX_TEST_UI_ENV", ""))
        persona = self._persona()
        stack_region = ui_env.get("VITE_AWS_REGION") or args.region
        self.summary["stack"]["region"] = stack_region
        run_dir = self._make_run_dir()
        target, facts = self._open_browser(tc, ui_env, persona, run_dir)
        withheld = self._video_withheld(ui_env, facts)
        session_dir, recorder_env = self._start_recording(target, flow_ids, run_dir)
        stack_facts = section.StackFacts(
            name=args.stack,
            region=stack_region,
            version=facts.version,
            build=facts.build,
        )
        self.summary["stack"].update(version=facts.version, build=facts.build)
        caveats = section.caveats_for(change, stack_facts)
        text = prompt.build_prompt(
            inputs=inputs,
            change=change,
            flows=chosen,
            stack=stack_facts,
            caveats=caveats,
            skills=_skills(),
            persona=args.persona,
            base_url=paths.UI_ORIGIN + "/",
            limits=prompt.Limits(
                max_flows=args.max_flows,
                minutes=args.session_minutes,
                max_turns=args.max_turns,
            ),
        )
        result, completed = self._session(tc, text, sts, identity, run_dir, setup_began)
        marks = self._end_recording(session_dir, recorder_env)
        audit = self._end_browser()
        if result is None or result.is_error:
            raise review.Failure(self._session_failure(result, completed))
        scrubbed = _scrubbed_report(result.text, self.scrub)
        self._write("report.md", scrubbed)
        report = section.parse_report(scrubbed)
        video = self._video(
            report,
            marks,
            session_dir,
            stack_facts,
            flow_ids,
            sts,
            identity,
            withheld=withheld,
        )
        note_caveats = self._note_caveats(caveats)
        return self._deliver(report, stack_facts, video, note_caveats, audit)

    def _note_caveats(self, caveats: list[str]) -> list[str]:
        """The prompt's caveats, and one for what the proxy refused the page itself.

        The proxy's refusals are final once it has stopped (:meth:`_end_browser`).
        Chrome's own background requests and the harness's proxy self-test are
        refused by design and say nothing about the MR, so only a refusal of what
        the page asked for or caused becomes a caveat, which is what a reader
        weighs a broken-looking screen against.
        """
        refused = section.refusal_caveat(
            self.browser.refusals, background=chrome.not_the_page
        )
        return [*caveats, refused] if refused else list(caveats)

    def _toolchain(self) -> paths.Toolchain:
        """The pinned toolchain and, unless unconfined, the installed policy."""
        args = self.args
        tc = paths.toolchain(args.toolchain_prefix, self.runtime.environ)
        absent = tc.missing()
        if absent:
            raise review.Failure(
                f"the UX toolchain under {tc.prefix} has no {', '.join(absent)}; "
                "run scripts/ux_runner/install.sh"
            )
        versions = tc.versions()
        self.summary["toolchain"] = {key: versions.get(key) for key in _TOOLCHAIN_KEYS}
        if args.dev_unconfined:
            self._say(
                "--dev-unconfined: no managed policy and no uid switch, so nothing "
                "is posted"
            )
        elif not chrome.policy_installed(chrome.load_policy()):
            raise review.Failure(
                "the managed Chrome policy is not installed as "
                "scripts/ux_runner/chrome_policy.json says; run "
                "scripts/ux_runner/install.sh"
            )
        return tc

    def _account(self) -> tuple[Any, account.Identity]:
        """The STS client, and the job's identity checked against the account."""
        rt = self.runtime
        sts = rt.sts(self.args.region)
        identity = account.caller_identity(sts)
        self.scrub.redactor.add(identity.arn)
        try:
            account.check_account(
                identity, rt.environ.get("UX_TEST_AWS_ACCOUNT_ID", "")
            )
        except account.AccountMismatch as mismatch:
            self.summary["stack"]["account_check"] = "mismatch"
            raise review.Skip(str(mismatch)) from None
        self.summary["stack"]["account_check"] = "matched"
        self._say(
            "account: the credentials reach the expected account, ending in "
            f"{identity.account[-4:]}"
        )
        return sts, identity

    def _flows(self, change: ui_change.UiChange) -> tuple[list[flows.Flow], list[str]]:
        """The catalogue flows the change touches, after the change itself."""
        args = self.args
        catalogue = flows.load_flows(paths.HARNESS_ROOT / FLOWS_FILE)
        chosen = flows.select_flows(
            catalogue,
            change.user_visible,
            persona=args.persona,
            limit=args.max_flows - 1,
        )
        flow_ids = ["change", *(flow.id for flow in chosen)]
        self.summary["flows"] = flow_ids
        self._say(f"flows: {', '.join(flow_ids)}")
        return chosen, flow_ids

    def _persona(self) -> signin.Persona:
        """The persona to sign in as, from ``UX_TEST_PERSONAS``."""
        persona_text = self.runtime.environ.get("UX_TEST_PERSONAS", "")
        persona = signin.personas_from_env(persona_text).get(self.args.persona)
        if persona is None:
            raise review.Skip(f"UX_TEST_PERSONAS has no {self.args.persona!r} persona")
        return persona

    def _make_run_dir(self) -> Path:
        """The run's private directory, then the check that the job has time left."""
        args = self.args
        base = "/tmp" if os.path.isdir("/tmp") else None  # nosec B108 - see _Run
        run_dir = Path(tempfile.mkdtemp(prefix="ux-test-", dir=base))
        self.run_dir = run_dir
        os.chmod(run_dir, 0o711)  # nosec B103 - traverse-only, no listing
        left = self.deadline - self.runtime.clock()
        if left < (args.session_minutes + _RESERVE_MINUTES) * 60:
            raise review.Skip(
                f"not enough time left in the job: {left / 60:.0f} min, and the "
                f"session needs {args.session_minutes} plus {_RESERVE_MINUTES}"
            )
        return run_dir

    def _open_browser(
        self,
        tc: paths.Toolchain,
        ui_env: Mapping[str, str],
        persona: signin.Persona,
        run_dir: Path,
    ) -> tuple[dict[str, Any], signin.PageFacts]:
        """The confined browser, signed in as the persona (``ConfinedBrowser.open``).

        The proxy, Chrome and its socket watch, both self-tests and the sign-in, in
        that order. ``browser_uid_switched`` and the viewport go into
        ``summary.json`` whether or not opening finishes, once each is known. The
        viewport must be :data:`paths.VIEWPORT` as the signed-in page measures it,
        the size the prompt states and the recording's click bounds assume, and
        ``--dev-unconfined`` measures it without requiring it, because a
        development host's Chrome draws a window frame of its own size.
        """
        args = self.args
        plan = session.BrowserPlan(
            toolchain=tc,
            run_dir=run_dir,
            ui_env=ui_env,
            stack=args.stack,
            persona=persona,
            persona_label=args.persona,
            build_dir=Path(args.build_dir),
            dev_unconfined=args.dev_unconfined,
        )
        try:
            opened = self.browser.open(plan)
        finally:
            self.summary["browser_uid_switched"] = self.browser.browser_uid_switched
            if self.browser.viewport is not None:
                self.summary["viewport"] = self.browser.viewport
        return opened.target, opened.facts

    def _video_withheld(
        self, ui_env: Mapping[str, str], facts: signin.PageFacts
    ) -> str:
        """Why this run's video is withheld, or ``""`` when it may be rendered.

        Decided once the page is signed in, before anything is recorded, from
        :func:`review_stack_problem`, and kept in ``summary.json``:
        ``stack.review_stack`` says whether every source named
        :data:`REVIEW_STACK`, and ``recording.video_withheld`` says why a video was
        withheld. With :data:`DEV_VIDEO_FLAG`, which forces a dry run, the video is
        rendered into the artifact directory all the same, and a ``WARNING:`` line
        says why its stack is not established as the review stack and that its
        frames are unredacted.
        """
        args = self.args
        problem = review_stack_problem(args.stack, ui_env, facts.stack_name)
        self.summary["stack"]["review_stack"] = not problem
        if not problem:
            return ""
        if args.dev_video_any_stack:
            self._say(
                f"WARNING: {DEV_VIDEO_FLAG}: {problem}; the video is rendered all the "
                f"same, its frames unredacted, into {self.artifacts}, and nothing is "
                "posted"
            )
            return ""
        self.summary["recording"]["video_withheld"] = problem
        self._say(f"video: withheld, because {problem}")
        return problem

    def _start_recording(
        self, target: Mapping[str, Any], flow_ids: list[str], run_dir: Path
    ) -> tuple[Path, dict[str, str]]:
        """The recorder on the signed-in page.

        Returns the session directory and the recorder's environment. The opening
        line says the review stack only when :meth:`_video_withheld` found it, so
        a video :data:`DEV_VIDEO_FLAG` keeps of another stack does not say so.
        """
        args, rt = self.args, self.runtime
        rec_out = run_dir / "recordings"
        self.rec_out = rec_out
        recorder_env = recording.recorder_env(
            artifacts.private_dir(run_dir / "recorder-home")
        )
        where = "the review" if self.summary["stack"]["review_stack"] else "another"
        started = rt.start_recorder(
            harness_root=paths.HARNESS_ROOT,
            out_dir=rec_out,
            stack=args.stack,
            persona=args.persona,
            flows=flow_ids,
            target_id=target["id"],
            say=(
                f"UX test of merge request {args.mr} on {where} stack, as "
                f"{args.persona}."
            ),
            env=recorder_env,
        )
        self._say("recording: started")
        return Path(started["session_dir"]), recorder_env

    def _session(
        self,
        tc: paths.Toolchain,
        text: str,
        sts: Any,
        identity: account.Identity,
        run_dir: Path,
        setup_began: float,
    ) -> tuple[agent.AgentResult | None, processes.Completed]:
        """The ``claude`` session, with a Bedrock-only session minted for it now.

        When the self-assume is refused, which is the usual case for a local run
        on a developer's SSO session, ``claude`` gets the job's own credentials
        (D5) and a ``WARNING:`` line says so before the session starts. Its working
        directory, home, temporary directory and the chrome-devtools file root are
        private directories of the run, and the MCP config is written ``0o600``
        beside them.
        """
        args, rt = self.args, self.runtime
        if self.rec_out is None:
            raise review.Failure("the session has no recorder to mark")
        bedrock = session.mint_bedrock(sts, identity, f"ux-test-{args.mr}")
        self._credential_scope("claude", bedrock, fallback="ambient")
        if bedrock is None:
            self._say(
                "WARNING: no Bedrock-only session could be minted, so claude runs on "
                "the job's own AWS credentials (credential_scope.claude: ambient)"
            )
        aws_env = bedrock if bedrock is not None else agent.aws_env_from(rt.environ)
        agent_root = artifacts.private_dir(run_dir / "agent")
        agent_cwd = artifacts.private_dir(agent_root / "cwd")
        agent_home = artifacts.private_dir(agent_root / "home")
        agent_tmp = artifacts.private_dir(agent_root / "tmp")
        mcp_files = artifacts.private_dir(agent_root / "mcp-files")
        mcp_path = agent.write_mcp_config(
            agent_root / "mcp.json",
            agent.mcp_servers(
                toolchain=tc,
                recorder_adapter=paths.HARNESS_ROOT / "scripts" / "ux_recorder_mcp.py",
                recorder_out=self.rec_out,
                python=sys.executable,
                home=agent_home,
                fs_root=mcp_files,
                search_path=_search_path(),
            ),
        )
        argv = agent.claude_argv(
            tc.claude,
            mcp_path,
            max_turns=args.max_turns,
            max_budget_usd=args.max_budget_usd,
        )
        env = agent.child_env(
            home=agent_home,
            tmpdir=agent_tmp,
            model=args.model,
            region=args.region,
            aws=aws_env,
            source=rt.environ,
        )
        self.timings["setup"] = rt.clock() - setup_began
        self._say(
            f"session: {len(tool_profiles.EXPERT_ALLOWED_TOOLS)} tools granted, "
            f"{len(tool_profiles.EXPERT_DISALLOWED_TOOLS)} denied by name; at most "
            f"{args.session_minutes} min, {args.max_turns} turns, "
            f"${args.max_budget_usd:.2f}; credentials "
            f"{self.summary['credential_scope']['claude']}"
        )
        began = rt.clock()
        result, completed = rt.run_agent(
            text, argv=argv, cwd=agent_cwd, env=env, timeout=args.session_minutes * 60
        )
        self.timings["session"] = rt.clock() - began
        self._note_result(result, completed)
        return result, completed

    def _end_recording(self, session_dir: Path, recorder_env: Mapping[str, str]) -> int:
        """Read the live mark count, stop the recorder, and return the marks.

        A recorder that no longer answers is counted from the timeline its stop
        finalised from disk. The live status also says how many click reports the
        recorder refused and how many DevTools messages it discarded for their
        size, both of which the page under test can cause.
        """
        rt = self.runtime
        if self.rec_out is None:
            raise review.Failure("there is no recorder to stop")
        live = rt.recorder_status(self.rec_out)
        rt.stop_recorder(
            harness_root=paths.HARNESS_ROOT,
            out_dir=self.rec_out,
            session_dir=session_dir,
            say=_RECORDER_STOP_SAY,
            env=recorder_env,
        )
        marks = live.get("marks") if isinstance(live, dict) else None
        if not isinstance(marks, int) or isinstance(marks, bool):
            marks = _marks_on_disk(session_dir)
        status = live if isinstance(live, dict) else {}
        self.summary["recording"].update(
            marks=marks,
            clicks_rejected=_count(status.get("clicks_rejected")),
            cdp_messages_dropped=_count(status.get("cdp_messages_dropped")),
        )
        self._say(f"recording: stopped, {marks} mark(s)")
        return marks

    def _end_browser(self) -> chrome.SocketAudit:
        """Terminate Chrome, then take the audit's verdict, then stop the proxy.

        Render needs only the session directory and the post only the GitLab API,
        so nothing after this runs beside a live page of the MR's signed in as the
        persona, nor beside any other process of the browser's uid. The verdict is
        noted (:meth:`_note_audit`) before the proxy stops, and the proxy's
        refusal log is final once it has (``ConfinedBrowser.close``).
        """
        return self.browser.close(on_audit=self._note_audit)

    def _deliver(
        self,
        report: section.Report,
        stack_facts: section.StackFacts,
        video: _Video,
        caveats: list[str],
        audit: chrome.SocketAudit,
    ) -> int:
        """Write the section locally, then post it when the run may post.

        ``section.md`` is written first in its local form, with the video named
        relative to the artifacts, so it exists whatever happens next; a post
        replaces it with the text that was posted. A socket the browser must not
        have opened fails the run in every mode, and so does a process of the
        browser's uid that had left Chrome's process group and was still running
        once ``chrome.kill_strays`` had waited for it. A dry run, and with it
        ``--dev-unconfined``, stops here. An audit that could not run posts nothing
        either, because a clean audit is the condition for posting, not the
        absence of a dirty one.
        """
        args = self.args
        shown = video.file if video.file is not None and video.attachable else None
        chapters = video.chapters if shown is not None else []
        local = self._section(
            report,
            stack_facts,
            video_line=f"![UX test]({shown.name})" if shown is not None else None,
            video_note=video.note,
            chapters=chapters,
            caveats=caveats,
            upload_id=None,
        )
        self._write("section.md", local)
        if audit.state == "violations":
            raise review.Failure(
                "the browser had a non-loopback socket: "
                + ", ".join(audit.violations[:5])
            )
        strays = self.browser.strays
        if strays:
            raise review.Failure(
                f"a process of {paths.BROWSER_USER} outlived Chrome's process group "
                "and was killed, so the run is not clean: " + ", ".join(strays[:5])
            )
        if args.dry_run:
            self.summary["recording"]["video_attached"] = shown is not None
            return self._finish(
                "dry_run", f"nothing posted; section.md is in {self.artifacts}"
            )
        if audit.state != "clean":
            raise review.Failure(
                "the socket audit was unavailable, so nothing is posted: it needs "
                f"Chrome to run as {paths.BROWSER_USER}, which takes a job running "
                "as root, and at least one pass that saw that uid's sockets"
            )
        return self._post(report, stack_facts, video, chapters, caveats)

    def _credential_scope(
        self, consumer: str, minted: Mapping[str, str] | None, *, fallback: str
    ) -> None:
        """Record whether ``consumer``'s session was scoped, and redact what was minted.

        One entry per consumer (``claude``, ``render``), because they are minted
        separately and either can fail alone: a run whose Bedrock session was
        scoped but whose Polly one was not must not read like one where ``claude``
        held the job's own credentials. ``fallback`` names what the consumer holds
        when nothing was minted: ``ambient`` for ``claude``, ``unavailable`` (no
        credential at all) for render. ``render`` stays ``None`` when narration
        was not asked for.
        """
        self.summary["credential_scope"][consumer] = (
            fallback if minted is None else "scoped"
        )
        if minted is not None:
            self.scrub.redactor.add(*minted.values())

    def _note_result(
        self, result: agent.AgentResult | None, completed: processes.Completed
    ) -> None:
        model = self.summary["model"]
        if result is None:
            model["stop"] = "timeout" if completed.timed_out else "error"
        else:
            model.update(
                cost_usd=result.cost_usd,
                num_turns=result.num_turns,
                duration_s=result.duration_s,
                stop=result.stop,
            )
        cost = model["cost_usd"]
        spent = f", ${cost:.2f}" if isinstance(cost, (int, float)) else ""
        self._say(f"session: ended ({model['stop']}){spent}")

    def _session_failure(
        self, result: agent.AgentResult | None, completed: processes.Completed
    ) -> str:
        if result is None and completed.timed_out:
            return f"the UX session timed out after {self.args.session_minutes} min"
        stop = result.stop if result is not None else "error"
        tail = completed.stderr.strip()[-_STDERR_CHARS:]
        message = f"the UX session failed: {stop}, exit {completed.returncode}"
        return f"{message}: {tail}" if tail else message

    def _note_audit(self, audit: chrome.SocketAudit) -> None:
        self.audit = audit
        found = self.browser.strays
        self.summary["socket_audit"] = {
            "state": audit.state,
            "violations": list(audit.violations),
            "sockets": audit.sockets,
            "strays": list(found),
        }
        strays = f", {len(found)} stray process(es) killed" if found else ""
        self._say(f"socket audit: {audit.state}{strays}")

    def _video(
        self,
        report: section.Report,
        marks: int,
        session_dir: Path,
        stack_facts: section.StackFacts,
        flow_ids: list[str],
        sts: Any,
        identity: account.Identity,
        *,
        withheld: str,
    ) -> _Video:
        """Render the mp4 when the change was seen and marked, and copy it out.

        Never when ``withheld`` says why not (:meth:`_video_withheld`): a run whose
        stack is not established as the review stack renders nothing, prepares
        nothing for render and mints render no session, so no mp4 of it exists to
        copy into the artifacts or upload, and the note says the video was
        withheld.

        Everything render turns into speech, captions or a card is redacted first:
        ``review.md``, whose Findings become the end card, and the recording's own
        text (:meth:`_scrub_session`). The Polly session is minted here, at the
        moment of use, and only when there is narration to speak; render runs as a
        child of its own, as ``uxrender`` when the harness can switch users,
        holding that session alone, and with no credential at all without
        narration or when no such session could be minted (:meth:`_polly_session`).
        It may take whatever is left before the deadline, and at least
        :data:`_RENDER_MIN_SECONDS`.

        By then the session directory is render's, so the mp4 and, for an
        attachable video, its chapters are read back only as regular files of
        render's own uid in that directory, through descriptors opened without
        following a link (``recording.copy_output``, ``recording.chapters``): a
        link, a pipe, another uid's file or an oversized one there fails the run.
        """
        args, rt = self.args, self.runtime
        if withheld:
            return _Video(None, False, _VIDEO_WITHHELD.format(reason=withheld))
        if not report.visible_change_found:
            return _Video(None, False, _NO_VISIBLE_CHANGE)
        if marks < 1:
            return _Video(None, False, _NO_MARKS)
        if self.run_dir is None:
            raise review.Failure("render has no run directory to log into")
        head = self.summary["head_sha"]
        recording.write_review_md(
            session_dir,
            self.scrub(
                section.review_md(
                    report,
                    stack=stack_facts,
                    persona=args.persona,
                    date=datetime.now(timezone.utc).date().isoformat(),
                    flows=flow_ids,
                )
            ),
        )
        self._scrub_session(session_dir)
        polly = None if args.no_narration else self._polly_session(sts, identity)
        render_home = artifacts.private_dir(self.run_dir / "render-home")
        began = rt.clock()
        mp4, narration = rt.render_video(
            session_dir,
            harness_root=paths.HARNESS_ROOT,
            voice=args.voice,
            region=args.region,
            narration=polly is not None,
            aws=polly or {},
            home=render_home,
            log_path=self.run_dir / "render.log",
            timeout=max(_RENDER_MIN_SECONDS, self.deadline - rt.clock()),
            search_path=_search_path(
                "ffmpeg", recording.recorder_env(render_home)["PATH"]
            ),
            user=paths.RENDER_USER,
        )
        self.timings["render"] = rt.clock() - began
        if polly is None and not args.no_narration:
            narration = _NARRATION_UNSCOPED
        self.summary["recording"]["narration"] = narration
        owner = recording.render_uid(paths.RENDER_USER)
        artifact = self.artifacts / f"ux-test-mr{args.mr}-{head[:8]}.mp4"
        size = (
            None
            if mp4 is None
            else recording.copy_output(
                session_dir,
                recording.VIDEO_FILE,
                lambda: artifacts.open_artifact(artifact),
                owner=owner,
                limit=recording.MAX_VIDEO_BYTES,
            )
        )
        if size is None:
            raise review.Failure("render finished without writing a video")
        self.summary["recording"]["video_bytes"] = size
        self._say(f"render: {size / _MIB:.1f} MB, narration {narration}")
        if size > args.max_upload_mb * _MIB:
            return _Video(
                artifact, False, _OVER_LIMIT.format(size=f"{size / _MIB:.0f}")
            )
        return _Video(artifact, True, None, rt.chapters(session_dir, owner=owner))

    def _polly_session(
        self, sts: Any, identity: account.Identity
    ) -> dict[str, str] | None:
        """Render's Polly-only session, minted now, or None when it cannot be had.

        There is no fallback to the job's own credentials, as there is for
        ``claude``: render decodes the page's frames and speaks the model's text,
        the most MR-influenced data the run handles, and a narration is not worth
        handing it everything the job holds. So a refused assume, or an identity
        with no role to re-assume, renders the video without narration, which
        ``summary.json`` records twice: ``credential_scope.render`` is
        ``unavailable`` and ``recording.narration`` is
        :data:`_NARRATION_UNSCOPED`.
        """
        polly = session.mint_polly(sts, identity, f"ux-test-{self.args.mr}-render")
        self._credential_scope("render", polly, fallback="unavailable")
        if polly is None:
            self._say(
                "render: no Polly-only session could be minted, so the video is "
                "rendered without narration and render holds no credential"
            )
        return polly

    def _scrub_session(self, session_dir: Path) -> None:
        """Redact the recording's text before render speaks, captions or titles it.

        Render takes every mark's ``say`` from ``timeline.json``, sends it to
        Polly and muxes it into the mp4 as a subtitle track, and takes the labels
        as its chapters; and it prefers the recorder's ``narration.md``, which the
        recorder wrote at stop from the same unredacted lines. So each event's text
        fields are redacted as decoded values, since a secret JSON-escaped in the
        file would not match its registered form, the timeline is written back,
        and ``narration.md`` is removed, so render speaks the redacted lines. The
        frames cannot be redacted, which the module docstring states.
        """
        path = session_dir / "timeline.json"
        try:
            timeline = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            raise review.Failure(
                "the recording's timeline cannot be read, so it is not rendered"
            ) from None
        events = timeline.get("events") if isinstance(timeline, dict) else None
        if not isinstance(events, list):
            raise review.Failure("the recording's timeline holds no events")
        for event in events:
            if not isinstance(event, dict):
                continue
            for key in _EVENT_TEXT_FIELDS:
                value = event.get(key)
                if isinstance(value, str):
                    event[key] = self.scrub(value)
        path.write_text(json.dumps(timeline, indent=2) + "\n", encoding="utf-8")
        (session_dir / _NARRATION_OVERRIDE).unlink(missing_ok=True)

    def _section(
        self,
        report: section.Report,
        stack_facts: section.StackFacts,
        *,
        video_line: str | None,
        video_note: str | None,
        chapters: list[tuple[str, str]],
        caveats: list[str],
        upload_id: int | None,
    ) -> str:
        """The section, redacted, with its marker and video line kept exact.

        The chapter labels (the model's mark labels) and the caveats (which name
        MR-written paths) are redacted as fields before ``compose_section``
        sanitises them, as the report's were, because sanitising respells a value
        the redactor would otherwise match. The whole section is redacted again
        after, in the form that cannot write markup (``artifacts.Scrub.note``).
        """
        if self.inputs is None:
            raise review.Failure("the section has no head to name")
        if video_line is not None:
            self.scrub.keep(video_line)
        labelled = [
            (self.scrub.field(at), self.scrub.field(label)) for at, label in chapters
        ]
        text = section.compose_section(
            head_sha=self.inputs.head_sha,
            persona=self.args.persona,
            stack=stack_facts,
            report=report,
            video=video_line,
            video_note=None if video_line is not None else video_note,
            chapters=labelled if video_line is not None else [],
            caveats=[self.scrub.field(caveat) for caveat in caveats],
            model=self.args.model,
            upload_id=upload_id,
        )
        marker = section.UX_MARKER_RE.search(text)
        if marker is not None:
            self.scrub.keep(marker.group(0))
        return self.scrub.note(text)

    def _post(
        self,
        report: section.Report,
        stack_facts: section.StackFacts,
        video: _Video,
        chapters: list[tuple[str, str]],
        caveats: list[str],
    ) -> int:
        """Append the section to this head's code-review note, and reply once.

        Only after the MR's head is confirmed unmoved and this job's notes hold no
        section for it. The code review is waited for (it is posted by the same
        token, so it is found by author id and full head SHA) and a standalone note
        is posted if it does not appear in time. The video is uploaded only after
        that wait, so a run that finds the head already tested while waiting
        uploads nothing; a section that lands between the last look and the edit
        still costs one upload, which is kept like every other (D9). An upload
        GitLab refuses or fails costs the section its video and nothing else
        (:meth:`_upload`). The wait is ``gitlab_api.wait_for_review_note``.
        """
        args, rt = self.args, self.runtime
        if self.inputs is None:
            raise review.Failure("the post has no head to name")
        head = self.inputs.head_sha
        credential = rt.environ.get(review.GITLAB.credential_env, "").strip()
        forge = _forge(args, rt, credential)
        began = rt.clock()
        waited = gitlab_api.wait_for_review_note(
            forge,
            args.mr,
            head,
            already_posted=lambda mine: any(
                section.has_section(_body(n), head) for n in mine
            ),
            poll_minutes=args.review_poll_minutes,
            deadline=self.deadline,
            clock=rt.clock,
            sleep=rt.sleep,
            poll_seconds=_POLL_SECONDS,
        )
        if waited.outcome == "moved":
            moved = waited.moved_to or ""
            return self._finish(
                "superseded",
                f"the MR head moved to {moved[:8]} before posting, so "
                f"nothing is posted for {head[:8]}",
            )
        if waited.outcome == "posted":
            self.summary["note"]["action"] = "unchanged"
            return self._finish("unchanged", "already tested")
        review_note, mine = waited.review_note, waited.mine
        line, upload_id, video_note = self._upload(forge, video)
        text = self._section(
            report,
            stack_facts,
            video_line=line,
            video_note=video_note,
            chapters=chapters if line is not None else [],
            caveats=caveats,
            upload_id=upload_id,
        )
        self._write("section.md", text)
        note = self.summary["note"]
        note["upload_id"] = upload_id
        self.summary["recording"]["video_attached"] = line is not None
        if review_note is not None:
            note_id = review_note.get("id")
            fresh = forge.note(args.mr, note_id)
            body = _body(fresh)
            if section.has_section(body, head):
                note["action"] = "unchanged"
                return self._finish("unchanged", "already tested")
            forge.update_note(args.mr, note_id, section.append_section(body, text))
            note.update(action="appended", note_id=note_id)
            self._reply(forge, note_id, report)
        else:
            created = forge.create_note(args.mr, section.compose_standalone(text))
            note_id = created.get("id")
            note.update(action="standalone", note_id=note_id)
        note["superseded"] = self._supersede(forge, mine, note_id, head)
        self.timings["post"] = rt.clock() - began
        where = (
            "appended to the code review"
            if review_note is not None
            else "posted on its own"
        )
        detail = f"UX test of {head[:8]} {where}"
        if note["upload_error"] is not None:
            detail += ", without its video, which is in the job artifacts"
        elif self.summary["recording"]["video_withheld"] is not None:
            detail += ", without its video, which was withheld"
        return self._finish("posted", detail)

    def _upload(
        self, forge: gitlab_api.UxGitlab, video: _Video
    ) -> tuple[str | None, int | None, str | None]:
        """Upload the mp4: its image line and upload id, or the sentence instead.

        A refused or failed upload costs the note its video, never its findings:
        the session they come from has already run and been paid for, and the mp4
        is in the job's artifacts either way. Whether the token's role may upload,
        and how large a file the instance accepts, are GitLab's to decide, so the
        reason is printed and kept in ``summary.json`` as ``note.upload_error``.
        """
        if video.file is None or not video.attachable:
            return None, None, video.note
        try:
            payload = forge.upload(video.file, filename=video.file.name)
            url = forge.checked_upload_url(payload)
        except (review.Skip, review.Failure) as error:
            self.summary["note"]["upload_error"] = str(error)
            self._say(f"upload: the video was not uploaded: {error}")
            return None, None, _UPLOAD_FAILED
        ident = payload.get("id")
        upload_id = (
            ident if isinstance(ident, int) and not isinstance(ident, bool) else None
        )
        return f"![UX test]({url})", upload_id, None

    def _reply(
        self, forge: gitlab_api.UxGitlab, note_id: Any, report: section.Report
    ) -> None:
        """One reply in the review note's thread, which is what notifies.

        The section is already in the note, so a reply that cannot be posted is
        reported and leaves the run posted.
        """
        if self.inputs is None:
            return
        try:
            thread = forge.discussion_id_for(self.args.mr, note_id)
            if thread is None:
                self._say("reply: the review note's thread was not found")
                return
            forge.reply(
                self.args.mr,
                thread,
                self.scrub.note(section.compose_reply(self.inputs.head_sha, report)),
            )
        except (review.Skip, review.Failure) as error:
            self._say(f"reply: not posted: {error}")

    def _supersede(
        self,
        forge: gitlab_api.UxGitlab,
        mine: list[dict[str, Any]],
        note_id: Any,
        head: str,
    ) -> list[int]:
        """Mark the video lines of up to ten older sections of this job superseded.

        Newest first, and only notes whose every UX marker names another head. The
        uploads stay; deleting them needs a role this token does not hold.
        """
        done: list[int] = []
        considered = 0
        for item in mine:
            if considered >= _SUPERSEDE_LIMIT:
                break
            ident = item.get("id")
            if ident == note_id or not isinstance(ident, int):
                continue
            body = _body(item)
            heads = [sha for sha, _rev in section.sections_in(body)]
            if not heads or head in heads:
                continue
            considered += 1
            replaced = section.supersede_video(body, head[:8])
            if replaced is None:
                continue
            try:
                forge.update_note(self.args.mr, ident, replaced)
            except (review.Skip, review.Failure) as error:
                self._say(f"supersede: note {ident} not updated: {error}")
                continue
            done.append(ident)
        return done

    def _cleanup(self) -> None:
        """Stop everything the run started, each step whatever the others did.

        The ``claude`` group is not here: ``processes.run_in_group`` kills it in its
        own ``finally``, on a timeout, on a cancel and after every exit, before
        :meth:`_run` sees the result. The recorder goes before Chrome, which it is
        attached to, and the socket watch after it, so that on this path too the
        audit's last pass comes after Chrome has exited. A cancel that arrives
        during the cleanup is recorded and the remaining steps still run. The four
        browser steps are :attr:`browser`'s, and each is a method here so that the
        line a failed step prints keeps naming it.
        """
        steps = (
            self._close_session,
            self._stop_recorder,
            self._stop_chrome,
            self._stop_watch,
            self._stop_proxy,
            self._copy_egress,
            self._remove_run_dir,
        )
        for step in steps:
            try:
                step()
            except (processes.Terminated, KeyboardInterrupt):
                self.cancelled = True
            except Exception as error:
                self._say(
                    f"cleanup: {step.__name__.strip('_')}: {type(error).__name__}"
                )

    def _close_session(self) -> None:
        self.browser.close_session()

    def _stop_watch(self) -> None:
        self.browser.stop_watch(on_audit=self._note_audit)

    def _stop_recorder(self) -> None:
        if self.rec_out is not None and (self.rec_out / _RECORDER_CURRENT).exists():
            self.runtime.kill_recorder(self.rec_out)

    def _stop_chrome(self) -> None:
        """Kill Chrome's group, then the browser uid's strays (``ConfinedBrowser``)."""
        self.browser.stop_chrome()

    def _stop_proxy(self) -> None:
        self.browser.stop_proxy()

    def _copy_egress(self) -> None:
        """The proxy's totals into the summary, its refusals into the artifacts.

        Read after the proxy has exited, because it writes its stats on SIGTERM.
        The summary also counts the refusals by whose request each was
        (``chrome.refusal_origin``), so the page's own stand apart from Chrome's
        background traffic and the self-test; ``refusals.jsonl`` keeps every one,
        each with that ``origin`` beside the proxy's own fields.
        Both files are the proxy's, read back as such, and each is judged alone:
        one that is refused is left out and a line says why, because the cleanup
        this runs in reports only an exception's type, and the other is copied all
        the same. So refused stats never cost the note's caveat the
        ``refusals.jsonl`` it points at.
        """
        proxy = self.browser.proxy
        if proxy is None:
            return
        copied: dict[str, Any] = {}
        try:
            stats = egress.read_stats(proxy.stats_file, owner=proxy.owner)
        except egress.ProxyError as refused:
            self._say(f"egress: not copied: {refused}")
        else:
            copied.update({key: stats[key] for key in _EGRESS_KEYS if key in stats})
        try:
            records = egress.read_refusals(proxy.refusal_log, owner=proxy.owner)
        except egress.ProxyError as refused:
            self._say(f"egress: not copied: {refused}")
        else:
            copied["by_origin"] = chrome.refusals_by_origin(records)
            lines = [
                json.dumps(self.scrub.tree(chrome.with_origin(r)), ensure_ascii=False)
                for r in records
            ]
            self._write("refusals.jsonl", "".join(line + "\n" for line in lines))
        self.summary["egress"] = copied

    def _remove_run_dir(self) -> None:
        run_dir, self.run_dir = self.run_dir, None
        if run_dir is None:
            return
        if self.args.keep_run_dir:
            self._say(f"run directory kept: {run_dir}")
            return
        shutil.rmtree(run_dir, ignore_errors=True)


def run(args: argparse.Namespace, runtime: Runtime) -> int:
    """The full run: precheck, confinement, session, render, post, artifacts.

    Exit 0 on success or a skip (1 on a skip with ``--fail-on-skip``) and 1 on a
    failure or a cancel. ``summary.json`` is written in every case.
    """
    return _Run(args, runtime).execute()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mr", type=int, required=True, help="the MR's iid")
    parser.add_argument(
        "--target-branch",
        default=review.DEFAULT_TARGET_BRANCH,
        help=f"the MR's target branch (default: {review.DEFAULT_TARGET_BRANCH})",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("CI_PROJECT_ID")
        or os.environ.get("CI_PROJECT_PATH")
        or "",
        help="numeric project id or full path (default: $CI_PROJECT_ID)",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("CI_API_V4_URL") or "",
        help="GitLab API base URL (default: $CI_API_V4_URL, which GitLab CI sets)",
    )
    parser.add_argument(
        "--remote", default="origin", help="git remote to fetch from (default: origin)"
    )
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=Path.cwd(),
        help="the MR's checkout, for git (default: the current directory)",
    )
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=None,
        help="the MR's UI build (default: <repo-dir>/src/ui/build)",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=Path("ux-tests"),
        help="where the section, report, summary and mp4 go (default: ux-tests)",
    )
    parser.add_argument(
        "--inputs",
        type=Path,
        default=None,
        help="read the MR's inputs from this file instead of collecting them; "
        "with --collect-only, the file to write (default: none)",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--collect-only",
        action="store_true",
        help="write the MR's inputs from git and the API, then stop",
    )
    modes.add_argument(
        "--precheck",
        action="store_true",
        help="decide on a bare python3, write precheck.json and the proceed "
        "marker, then stop",
    )
    parser.add_argument(
        "--stack",
        default=REVIEW_STACK,
        help=f"the stack to test against (default: {REVIEW_STACK}, the review "
        "stack, the only one whose video is rendered)",
    )
    parser.add_argument(
        "--persona",
        default=DEFAULT_PERSONA,
        help=f"the UX_TEST_PERSONAS entry to sign in as (default: {DEFAULT_PERSONA})",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("ANTHROPIC_MODEL") or review.DEFAULT_MODEL,
        help=f"Bedrock model id (default: $ANTHROPIC_MODEL or {review.DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or _DEFAULT_REGION,
        help=f"region of Bedrock, STS and Polly (default: $AWS_REGION or "
        f"{_DEFAULT_REGION})",
    )
    parser.add_argument(
        "--max-flows",
        type=int,
        default=3,
        help="flows to look at, the change itself included (default: 3)",
    )
    parser.add_argument(
        "--max-turns", type=int, default=150, help="session turn cap (default: 150)"
    )
    parser.add_argument(
        "--max-budget-usd",
        type=float,
        default=10.0,
        help="session spending cap in USD (default: 10.0)",
    )
    parser.add_argument(
        "--session-minutes",
        type=int,
        default=25,
        help="session time cap (default: 25)",
    )
    parser.add_argument(
        "--deadline-minutes",
        type=int,
        default=55,
        help="minutes after the job started by which the run must be done "
        "(default: 55)",
    )
    parser.add_argument(
        "--review-poll-minutes",
        type=int,
        default=10,
        help="how long to wait for the code-review note before posting on its own "
        "(default: 10)",
    )
    parser.add_argument(
        "--max-upload-mb",
        type=int,
        default=95,
        help="the largest video uploaded to the MR, in MiB (default: 95)",
    )
    parser.add_argument(
        "--voice", default="Ruth", help="Amazon Polly voice (default: Ruth)"
    )
    parser.add_argument(
        "--no-narration", action="store_true", help="render the video without a voice"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="write section.md, and the review stack's mp4, to the artifact dir "
        "instead of posting",
    )
    parser.add_argument(
        "--fail-on-skip",
        action="store_true",
        help="exit 1 instead of 0 when a precondition is missing",
    )
    parser.add_argument(
        "--dev-unconfined",
        action="store_true",
        help="development only: no managed policy or uid switch; forces --dry-run",
    )
    parser.add_argument(
        DEV_VIDEO_FLAG,
        action="store_true",
        help="development only: render the video even when the stack is not "
        f"{REVIEW_STACK}, whose frames are then another stack's screens, "
        "unredacted; forces --dry-run, and is refused under CI",
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
        help="keep the run's private directory for debugging",
    )
    args = parser.parse_args(argv)
    if args.mr < 1:
        parser.error("--mr must be a positive iid")
    if args.max_flows < 1:
        parser.error("--max-flows counts the change itself, so it is at least 1")
    if args.max_turns < 1:
        parser.error("--max-turns must be at least 1")
    if not (math.isfinite(args.max_budget_usd) and args.max_budget_usd > 0):
        parser.error("--max-budget-usd must be a positive, finite amount")
    if args.session_minutes < 1:
        parser.error("--session-minutes must be at least 1")
    if args.build_dir is None:
        args.build_dir = Path(args.repo_dir) / "src" / "ui" / "build"
    if args.dev_unconfined or args.dev_video_any_stack:
        args.dry_run = True
    return args


#: The umask every file and directory this job makes is created under, whatever
#: the caller's: a GitLab Docker executor runs a job at umask 0000 by default.
_UMASK = 0o022


def _ci_variable(environ: Mapping[str, str]) -> str:
    """Whichever of :data:`CI_VARIABLES` is set, or ``""`` outside CI."""
    names = [name for name in CI_VARIABLES if environ.get(name, "").strip()]
    return names[0] if names else ""


def main(argv: list[str] | None = None, runtime: Runtime | None = None) -> int:
    """Set the umask and the SIGTERM handler, then collect, precheck or run.

    Under the umask a GitLab Docker executor leaves a job, every directory the
    run makes, and every socket, would be writable by the browser's and the
    proxy's uids: the recorder's control socket and its session among them, which
    root reads and renders from later. Both are put back on the way out, so a
    caller that imports this module keeps its own.

    :data:`DEV_VIDEO_FLAG` is refused before anything runs wherever GitLab CI's
    own ``CI`` or ``GITLAB_CI`` is set, read from the runtime's environment: a
    pipeline's artifacts can be downloaded, so the video of another stack is a
    local dry run's alone.
    """
    previous_umask = os.umask(_UMASK)
    restore = processes.install_sigterm_handler()
    try:
        args = parse_args(argv)
        chosen = runtime if runtime is not None else default_runtime()
        ci = _ci_variable(chosen.environ)
        if args.dev_video_any_stack and ci:
            print(
                f"FAILED: {DEV_VIDEO_FLAG} is a developer's opt-in for a local dry "
                f"run, and {ci} is set, so this is CI",
                flush=True,
            )
            return 1
        if args.collect_only:
            return _collect_main(args, chosen)
        if args.precheck:
            return _precheck_main(args, chosen)
        return run(args, chosen)
    except (processes.Terminated, KeyboardInterrupt):
        print("FAILED: cancelled", flush=True)
        return 1
    finally:
        restore()
        os.umask(previous_umask)


if __name__ == "__main__":
    sys.exit(main())
