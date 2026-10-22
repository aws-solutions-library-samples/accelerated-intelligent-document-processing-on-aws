#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""First-time-user stories, each judged from the journal of its own tool calls.

This is the unattended form of ``.claude/skills/ux-test-stories.md``, under the
contract in ``.claude/skills/ux-test-stories-ci.md``. A **story** in
``scripts/ux_stories.toml`` says only what a person wants; one story attempted as
one persona is a **flow**, keyed ``story@persona``. For every flow this opens one
confined, signed-in headless Chrome (``ux_runner.session``), hands a pinned
``claude -p`` exactly one MCP server, ``ux-story`` (``scripts/ux_story_mcp.py``
in relay mode), and judges the flow from the journal that server wrote
(``ux_runner.story_verdict``), never from the model's prose.

Modes
-----
``--story ID [--as PERSONA]``
    One story's flows. ``make ai-ux-test-stories-local`` runs this in Docker.
``--all [--tier p0|p1|all] [--shard i/n]``
    The nightly set, or one shard of it. Under GitLab CI it is the nightly
    shard, which re-runs a flow that gave up or ran out of steps at twice its
    budget; anywhere else it is a local run of the same flows.
``--mr IID``
    The flows the merge request's added CHANGELOG entries select
    (``ux_runner.changelog_select``), against the MR's UI build when it changes
    ``src/ui`` and otherwise against the deployed UI with the MR head's docs.
    ``--collect-only`` writes the MR's ``inputs.json`` (StoryInputs v1) from git.
``--replay RUN_DIR``
    The stories, personas, model, effort, budgets, docs and UI source an earlier
    run's ``story-run.json`` records, run again.
``--interactive --story ID --as PERSONA``
    Watch mode, run inside the browser container that ``run_in_docker.py``
    started and signed in: the pinned Claude TUI plays the user through the same
    server, a person reads along, and the host stops the container and judges.
``--judge RUN_DIR [--check] [--summary] [--audit FILE]``
    Re-judge a run from ``story-run.json`` and its journals: ``results.json`` and
    ``section.md``, byte for byte what the run wrote. Standard library only, so it
    runs on any host.
``--merge DIR... --out DIR`` and ``--digest DIR --out DIR``
    Several shards' runs as one, and the nightly digest over them, compared with
    the earlier scheduled nights' digests.
``--precheck``
    The CI job's decision on a bare ``python3``, before the toolchain is
    installed: ``precheck.json`` and, when the run should go ahead, ``proceed``.

Properties it is built around
-----------------------------
1. **Every flow is confined as the MR UX test's one session is.** A fresh run
   directory under ``/tmp`` and so a fresh Chrome profile, the egress proxy as
   the browser's only network path, one socket audit for each Chrome from its
   launch until after it has exited, the persona signed in over DevTools before
   anything records, and a Bedrock-only session minted for that flow alone. Render
   gets a Polly-only session, or, when none can be minted, no credential and no
   narration, unless ``--narrate-with-own-credentials`` asks for the run's own on a
   developer's machine. The
   browser is closed, its audit taken and its proxy stopped before the next
   flow starts, and nothing is rendered until every Chrome is gone. A socket the
   browser must not have opened, or a process of its uid that outlived it, fails
   the whole run in every mode, and posting needs every audit clean.
2. **The model holds the story server and nothing else.** ``claude`` runs with
   ``--tools ''``, ``--strict-mcp-config`` and the ``tool_profiles.STORY`` lists;
   its one server starts the pinned chrome-devtools-mcp itself and serves twelve
   of its tools behind the step budget, the image cap and the commit guard
   (``ux_runner.story_relay``). Every server runs under ``env -i``, the prompt
   goes on stdin (a watched run's goes in a private file the TUI appends to its
   system prompt, ``agent.interactive_argv``), and no MR text reaches the prompt:
   the CHANGELOG delta only selects stories, and the stories come from the
   catalogue in the harness.
3. **The verdict is a function of the journal.** The server is the journal's one
   writer; this process copies it into the artifacts through the redactor and
   judges that copy, so ``--judge --check`` on the artifacts reproduces
   ``results.json`` and ``section.md`` exactly. ``story-run.json`` holds the rest
   of what judging needs and no username, password or account id.
4. **Spend is counted twice and capped once.** A flow's cost is the larger of
   ``claude``'s own ``total_cost_usd`` and its token usage priced at
   :data:`MODEL_RATES`, so a model the pinned CLI misprices cannot carry a job
   past its cap unseen, and a model with no rates there is refused at the
   precheck. A flow starts only while the job's remaining budget
   covers that flow's cap and the time left covers its wall clock and the
   reserve. A bad verdict (``stuck``, ``not_refused``, ``offered``) runs once
   more with a fresh browser and session where both allow; a single bad result is
   labelled unconfirmed rather than left to look confirmed.
5. **A video is made of the review stack alone, decided per flow.** The frames
   are not redacted, so a flow's recording is rendered only when
   ``session.review_stack_problem`` finds ``--stack``, the settings parameter
   ``UX_TEST_UI_ENV`` names and the stack that flow's signed-in page reports all
   naming ``session.REVIEW_STACK``. That is the MR UX test's own check, called,
   and it is decided as soon as each flow's persona is signed in, before
   anything records. A flow it refuses runs and is judged like any other,
   records nothing and renders nothing, and the reply names that flow and the
   reason. No option renders a video of another stack.
6. **Nothing is posted without ``--post``.** Posting is a merge request run's
   alone, against the review stack, with every flow's audit clean; the reply
   goes in the code-review note's thread and that note is never edited. Every
   other mode, and every run without ``--post``, is a dry run whose
   ``section.md`` is what would have been posted.
7. **A sign-in is never retried.** The first refused sign-in for a Cognito group
   marks the remaining flows of that group ``not_run (sign_in_refused)``, because
   every flow signs in afresh and repeated failures lock the account out. That
   bound is per process: each ``--jobs`` container and each nightly shard signs in
   on its own, so a stale password is tried once by each of them that runs the
   group.

What it does NOT protect, stated rather than implied
----------------------------------------------------
Everything ``scripts/sdlc/ai_ux_test.py`` states under the same heading holds
here too: the trust root is the MR's own pipeline definition, the MR's UI code
holds the persona's credentials while it runs, the review stack is known by its
name rather than by its account or region, the socket audit polls, the proxy
confines Chrome as configured, loopback is only as private as the job's network
namespace, and root's children share root's ``/proc``. Beside those:

* **Screenshots are not held to the review-stack check.** A flow's ``screens/``
  are kept whatever its stack, so a run is pointed only at a stack holding
  shipped example data.
* **Look-only is partly an instruction.** The commit guard judges a click by the
  control's name and a key that presses or submits by the element that has focus,
  against the page as it is when the call arrives, and cannot see drag autosave,
  an unnamed control that is not beside a text box, or a full-page form's final
  button.
* **A watched run can be driven from ``chrome://inspect``**, which nothing
  detects, so a watched run is labelled ``watch`` and never compared.

Usage
-----
    # CI: the precheck on a bare python3, then the run when it wrote proceed
    python3 -B "$UX_HARNESS_DIR/$UX_ENTRY" $UX_MODE_ARGS --precheck \\
        --artifact-dir "$UX_ARTIFACTS"
    python3 -B "$UX_HARNESS_DIR/$UX_ENTRY" $UX_MODE_ARGS \\
        --inputs "$UX_ARTIFACTS/inputs.json" --artifact-dir "$UX_ARTIFACTS"

    # locally, in Docker (the make target builds this)
    make ai-ux-test-stories-local STORY=route-folder-to-profile AS=platform-admin

    # re-judge a run on any host, and check it against what it wrote
    python3 -B scripts/sdlc/ai_ux_test_stories.py --judge <run dir> --check

Environment
-----------
``UX_TEST_UI_ENV``, ``UX_TEST_PERSONAS``, ``UX_TEST_AWS_ACCOUNT_ID``
    As the MR UX test reads them. A ``UX_TEST_WEB_URL=`` line in ``UX_TEST_UI_ENV``
    names the deployed stack's web URL when ``--web-url`` does not.
``GITLAB_REVIEW_TOKEN``, ``CI_API_V4_URL``, ``CI_PROJECT_ID``
    A merge request run's API access: to read the MR and its notes, and with
    ``--post`` to reply. A scheduled ``--digest`` uses the same token only to
    read: develop's earlier scheduled pipelines and each one's ``digest.json``.
``UX_TEST_STORIES_SLACK_WEBHOOK_URL``
    Optional. A scheduled ``--digest`` posts its one-line summary to this Slack
    Workflow Builder webhook; without it nothing is posted and nothing is said.
``ANTHROPIC_MODEL``, ``AWS_REGION`` / ``AWS_DEFAULT_REGION``
    The Bedrock model id and the region of Bedrock, STS and Polly.
``UX_HARNESS``, ``UX_ENTRY``
    ``--precheck`` fails unless they name exactly :data:`HARNESS_PATHS`, in order,
    and :data:`ENTRY`.
``UX_HARNESS_SHA``
    The harness commit, for a harness tree with no git of its own (a CI job's
    ``git archive`` extraction); 40 hex digits, optionally ``+dirty``.
``CI_JOB_STARTED_AT``, ``CI_COMMIT_SHA``, ``CI_MERGE_REQUEST_SOURCE_BRANCH_SHA``,
``CI_PIPELINE_ID``, ``CI_PIPELINE_SOURCE``, ``CI_JOB_URL``, ``CI``, ``GITLAB_CI``
    Supplied by GitLab CI: the deadline counts from the job's start, a newer MR
    head stands this pipeline down, ``--all`` is the nightly shard where ``CI``
    or ``GITLAB_CI`` is set, and ``--dev-unconfined`` and
    ``--narrate-with-own-credentials`` are refused there. A digest
    reads its history and posts to Slack only where ``CI_PIPELINE_SOURCE`` is
    ``schedule``, leaves its own pipeline out of the history, and links its job.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess  # nosec B404 - the watched TUI only, a fixed argv and no shell
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from ux_runner import python_floor  # noqa: E402

python_floor.require(__file__)

import ux_test_proxy  # noqa: E402
from ux_runner import (  # noqa: E402
    account,
    agent,
    artifacts,
    changelog_select,
    chrome,
    docs_index,
    egress,
    gitlab_api,
    mr_inputs,
    paths,
    processes,
    readback,
    recording,
    section,
    session,
    signin,
    stories,
    story_digest,
    story_inputs,
    story_prompt,
    story_relay,
    story_section,
    story_verdict,
    tool_profiles,
    transcript,
    ui_change,
)

review = gitlab_api.review

#: Every path the CI jobs extract from the target branch with ``git archive`` and
#: run from: this file, its imports, the server and the recorder the flows run,
#: the catalogue, what builds the docs index, and the two skills the prompt
#: inlines. A test closes it over the imports of every module it names, and each
#: story job's ``UX_HARNESS`` must equal it.
HARNESS_PATHS = (
    "scripts/sdlc/ai_ux_test_stories.py",
    "scripts/sdlc/ai_mr_review.py",
    "scripts/ux_runner",
    "scripts/ux_test_proxy.py",
    "scripts/ux_recorder.py",
    "scripts/ux_recorder_cdp.py",
    "scripts/ux_recorder_render.py",
    "scripts/ux_recorder_mcp.py",
    "scripts/ux_story_mcp.py",
    "scripts/ux_stories.toml",
    "scripts/check_markdown_links.py",
    "docs-site/setup.sh",
    ".claude/skills/ux-test-stories.md",
    ".claude/skills/ux-test-stories-ci.md",
)
#: This file, as a story job's ``UX_ENTRY`` must name it, relative to the tree.
ENTRY = "scripts/sdlc/ai_ux_test_stories.py"
#: The operator skill, whose brief the prompt inlines.
OPERATOR_SKILL = ".claude/skills/ux-test-stories.md"
#: The unattended contract, which the prompt inlines whole.
CI_CONTRACT = ".claude/skills/ux-test-stories-ci.md"
#: The story server, run by path from the harness.
STORY_SERVER_SCRIPT = "scripts/ux_story_mcp.py"
DEFAULT_MODEL = "us.anthropic.claude-opus-5-5"
DEFAULT_EFFORT = "high"
#: $ per token for each model a story may run on, equal to
#: ``config_library/pricing.yaml``'s ``bedrock/<model>`` entry (a test pins
#: that), so the harness prices usage without reading a file outside itself.
MODEL_RATES: dict[str, dict[str, float]] = {
    "us.anthropic.claude-opus-5-5": {
        "input": 4.0e-6,
        "output": 2.0e-5,
        "cache_read": 2.0e-7,
        "cache_write": 5.0e-6,
    },
    "global.anthropic.claude-opus-5-5": {
        "input": 4.0e-6,
        "output": 2.0e-5,
        "cache_read": 2.0e-7,
        "cache_write": 5.0e-6,
    },
    "us.anthropic.claude-opus-5": {
        "input": 5.0e-6,
        "output": 2.5e-5,
        "cache_read": 5.0e-7,
        "cache_write": 6.25e-6,
    },
    "global.anthropic.claude-opus-5": {
        "input": 5.0e-6,
        "output": 2.5e-5,
        "cache_read": 5.0e-7,
        "cache_write": 6.25e-6,
    },
}
#: Set by GitLab CI in every job. Where either is set, ``--all`` is the nightly
#: shard rather than a local run.
CI_VARIABLES = ("CI", "GITLAB_CI")
#: The ``UX_TEST_UI_ENV`` line that names the deployed stack's web URL, which the
#: stack's ``WebUITestEnvFile`` output does not carry.
WEB_URL_KEY = "UX_TEST_WEB_URL"
INPUTS_SCHEMA = story_inputs.INPUTS_SCHEMA
INPUTS_VERSION = story_inputs.INPUTS_VERSION
#: Which flows a run renders a video of: those with a finding (and at night only
#: confirmed bad ones), every flow, or none. The review-stack check applies to
#: each whatever this says.
VIDEO_POLICIES = ("findings", "all", "never")
#: The most videos one run renders and one reply carries.
MAX_VIDEOS = 2
DEFAULT_RUN_DIR_NAME = "ux-test-stories"
#: The merge request job, whose earlier runs on an MR count toward ``mr_runs``
#: whether they posted or not, and how long counting them may take.
MR_JOB = "ai_ux_test_stories"
RUNS_COUNT_SECONDS = 60.0
#: The nightly digest's job and its ``--out`` directory: a later night reads
#: :data:`DIGEST_ARTIFACT` back from that job's artifacts as its history.
DIGEST_JOB = "ai_ux_test_stories_digest"
DIGEST_DIR_NAME = "ux-test-stories-digest"
DIGEST_ARTIFACT = f"{DIGEST_DIR_NAME}/digest.json"
#: The branch whose scheduled pipelines are the nights a digest is compared with.
HISTORY_REF = review.DEFAULT_TARGET_BRANCH
#: How long reading the earlier nights may take; a night not reached by then is
#: a gap in the history.
HISTORY_SECONDS = 300.0
#: The optional Slack Workflow Builder webhook a scheduled digest posts its
#: summary to, and how long that post may take.
SLACK_WEBHOOK_ENV = "UX_TEST_STORIES_SLACK_WEBHOOK_URL"
SLACK_SECONDS = 30.0

_DEFAULT_REGION = "us-east-1"
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_HARNESS_SHA_RE = re.compile(r"[0-9a-f]{40}(?:\+dirty)?")
_ACCOUNT_ID_RE = re.compile(r"[0-9]{12}")
_PIPELINE_ID_RE = re.compile(r"[0-9]{1,18}")
_PROCEEDED_RE = re.compile(rb"PROCEED: [0-9]+ flow\(s\) of [0-9]+ stor")
_SHARD_RE = re.compile(r"([1-9][0-9]{0,2})/([1-9][0-9]{0,2})")
_CFT_PIN_RE = re.compile(r'^CFT_VERSION="([0-9]+(?:\.[0-9]+){3})"$', re.M)
_SCREEN_NAME_RE = re.compile(r"[0-9]{3,6}\.jpg")
_SCREENS_DIR = "screens"
#: What stops a flow that a re-run at twice the budget can get past.
_EXTENDED_CAUSES = ("budget_exhausted", "max_turns", "dollar_cap")
_RENDER_MIN_SECONDS = 300.0
_RENDER_MAX_SECONDS = 1800.0
_POLL_SECONDS = gitlab_api.POLL_SECONDS
_SUPERSEDE_LIMIT = 10
_MIB = 1024 * 1024
_RECORD_MAX_BYTES = 8 * _MIB
_SUMMARY_MAX_BYTES = 1 * _MIB
_SCREEN_BYTES = story_relay.SCREEN_BYTES
_STDERR_CHARS = 400
_SLACK_ANSWER_BYTES = 4096
_WEBHOOK_CHARS = 2048
_RECORDER_CURRENT = "current.json"
_RECORDER_STOP_SAY = "That ends the story."
_NARRATION_OVERRIDE = "narration.md"
_NO_UI_ENV = (
    "UX_TEST_UI_ENV is not set, so the browser's egress rules cannot be derived "
    "from the stack's UI environment"
)
_EVENT_TEXT_FIELDS = ("label", "say", "text", "target", "under", "interactive")
_TOOLCHAIN_KEYS = ("claude_code", "chrome_devtools_mcp", "chrome")
_UNKNOWN_VERSION = "unknown"
_INPUTS_PROSE_FIELDS = ("title", "author", "web_url", "source_branch", "ui_diff")
_FOOTER_PREFIX = "🤖 Automated first-time-user stories of "
_WITHHELD_LINE = "_The video of `{key}` was withheld, because {reason}._"
_WITHHELD_WHY = (
    "_A story's video is made only of the review stack `{stack}`, because its "
    "frames are not redacted._"
)
_LOCAL_VIDEO_LINE = "_Video of `{key}`, not uploaded in a dry run: `{name}`._"
_UPLOAD_FAILED_LINE = (
    "_The video of `{key}` could not be uploaded; it is in the job artifacts._"
)
_OVER_LIMIT_LINE = (
    "_The video of `{key}` ({size} MB) is over the upload limit; it is in the job "
    "artifacts._"
)
_NOT_RENDERED_LINE = "_The video of `{key}` could not be rendered._"
_DROPPED_CAVEAT = "not run as {persona}: UX_TEST_PERSONAS has no {group} persona"
_SINCE_CAVEAT = (
    "the stack's version could not be read, so no story's `since` was checked"
)
_SIGN_IN_REFUSED = "sign-in was refused"
_BROWSER_STEPS = (
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
)
_STEP_ERRORS = {
    "launch_proxy": "launch",
    "launch_chrome": "launch",
    "socket_watch": "launch",
    "wait_for_cdp": "launch",
    "first_page_target": "launch",
    "open_session": "launch",
    "verify_policy": "selftest",
    "verify_proxy_path": "selftest",
    "sign_in": "sign_in",
    "page_facts": "viewport",
    "verify_viewport": "viewport",
    "measure_viewport": "viewport",
}


@dataclass
class Runtime:
    """Every collaborator that touches a process, the network, AWS or the clock.

    The thirteen fields ``session.BrowserRuntime`` names are here under the same
    names, so a flow's ``ConfinedBrowser`` is handed this as it is, and each other
    field defaults (in :func:`default_runtime`) to the module function of the
    same job, so the tests drive :func:`main` end to end with fakes and no
    subprocess, network or AWS call.
    """

    forge: Callable[[argparse.Namespace, str], gitlab_api.UxGitlab]
    collect_story: Callable[..., Mapping[str, Any]]
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
    run_tui: Callable[..., processes.Completed]
    render_video: Callable[..., tuple[Path | None, str]]
    chapters: Callable[..., list[tuple[str, str]]]
    index_docs: Callable[..., DocsCorpus]
    compose_prompt: Callable[..., str]
    harness: Callable[[Mapping[str, str]], tuple[str, bool | None]]
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    wall: Callable[[], float] = time.time
    environ: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    history: (
        Callable[[argparse.Namespace, Mapping[str, str]], story_digest.History | None]
        | None
    ) = None
    slack: Callable[[str, Mapping[str, str]], str] | None = None


@dataclass(frozen=True)
class DocsCorpus:
    """The documentation a run's flows read: its index, and the tree it came from.

    ``tree`` is the 40-hex id of the ``docs`` tree at the commit the index was
    built from, or ``worktree`` for a checkout's files.
    """

    index: docs_index.Index
    tree: str

    @property
    def ref(self) -> str | None:
        """The commit the docs were read from, or None for a working tree."""
        return self.index.source.ref

    @property
    def journal_ref(self) -> str:
        """The journal's ``docs_ref``: the commit, ``worktree`` or ``worktree+dirty``."""
        if self.index.source.ref is not None:
            return self.index.source.ref
        return "worktree+dirty" if self.index.source.dirty else "worktree"


#: A merge request's StoryInputs v1, as ``inputs.json`` holds it: the record
#: ``ux_runner.story_inputs`` collects from git and reads back.
StoryInputs = story_inputs.StoryInputs


class StoryRunError(review.Failure):
    """A story run cannot go on; the text names the step and why."""


def _say(scrub: artifacts.Scrub, text: str) -> None:
    print(scrub(text), flush=True)


def _unexpected(scrub: artifacts.Scrub, error: BaseException) -> str:
    """Print a redacted traceback to stderr, and return the one-line reason.

    Python's own handler would print the traceback unredacted, and an exception's
    text can carry whatever the call that raised it was handling.
    """
    trace = "".join(traceback.format_exception(error))
    print(scrub(trace), file=sys.stderr, flush=True)
    return f"{type(error).__name__}: {error}"


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


def _pipeline_id(environ: Mapping[str, str]) -> int | None:
    """This pipeline's id (``CI_PIPELINE_ID``), or None outside CI."""
    pipeline = environ.get("CI_PIPELINE_ID", "").strip()
    return int(pipeline) if _PIPELINE_ID_RE.fullmatch(pipeline) else None


def _pipeline_head(environ: Mapping[str, str]) -> str | None:
    """The MR head this pipeline, and so its UI build, is of; None outside CI."""
    for name in ("CI_MERGE_REQUEST_SOURCE_BRANCH_SHA", "CI_COMMIT_SHA"):
        value = environ.get(name, "").strip()
        if value:
            return value if _SHA_RE.fullmatch(value) else None
    return None


def _ci_variable(environ: Mapping[str, str]) -> str:
    """Whichever of :data:`CI_VARIABLES` is set, or ``""`` outside CI."""
    names = [name for name in CI_VARIABLES if environ.get(name, "").strip()]
    return names[0] if names else ""


def _search_path(tool: str = "node", base: str = agent.DEFAULT_SEARCH_PATH) -> str:
    """``PATH`` for a child: the directory ``tool`` is found in here, then ``base``.

    The story server starts chrome-devtools-mcp, which needs ``node``, and render
    needs ffmpeg; on a development host either can live outside ``base``.
    """
    found = shutil.which(tool)
    if not found:
        return base
    tool_dir = str(Path(found).parent)
    if tool_dir in base.split(os.pathsep):
        return base
    return f"{tool_dir}{os.pathsep}{base}"


def _body(note: Mapping[str, Any]) -> str:
    body = note.get("body")
    return body if isinstance(body, str) else ""


def _git(repo_dir: Path, *args: str) -> str:
    """``git`` in ``repo_dir``, trusting that one directory whoever owns it.

    In the Docker runner the checkout is mounted from the host and owned by the
    host's user, which git refuses to read as root without ``safe.directory``.
    Only that exact path is named, and only read-only subcommands are run.
    """
    where = str(Path(repo_dir).resolve())
    return ui_change.git(Path(repo_dir), "-c", f"safe.directory={where}", *args)


def pinned_cost(model: str, usage: Mapping[str, int] | None) -> float | None:
    """``usage`` priced at :data:`MODEL_RATES`, in dollars; None without both.

    A count ``usage`` lacks is priced as none of that kind, so the figure is a
    floor; the budget counts the larger of it and ``claude``'s own.
    """
    rates = MODEL_RATES.get(model)
    if rates is None or not usage:
        return None
    total = (
        usage.get("input_tokens", 0) * rates["input"]
        + usage.get("output_tokens", 0) * rates["output"]
        + usage.get("cache_read_input_tokens", 0) * rates["cache_read"]
        + usage.get("cache_creation_input_tokens", 0) * rates["cache_write"]
    )
    return round(total, 6)


def counted_cost(cost_usd: float | None, pinned: float | None, cap: float) -> float:
    """What the job's budget subtracts for one flow: the larger known figure.

    With neither known, the flow is counted at its whole ``cap``, because a flow
    whose cost cannot be read must not look free.
    """
    known = [value for value in (cost_usd, pinned) if value is not None]
    return round(max(known), 6) if known else round(float(cap), 6)


def read_texts() -> story_prompt.PromptTexts:
    """The brief and the unattended contract as every story prompt inlines them.

    Read from the harness tree (``story_prompt.read_texts``), so the MR under test
    cannot rewrite its own instructions, and refused rather than altered when
    either cannot be inlined verbatim.
    """
    try:
        return story_prompt.read_texts()
    except story_prompt.StoryPromptError as error:
        raise StoryRunError(f"the story skills cannot be inlined: {error}") from None


def pinned_versions(root: Path | None = None) -> dict[str, str]:
    """The three versions a series names, as the harness pins them before install.

    ``VERSIONS.json`` records what ``install.sh`` installed, which is exactly the
    lockfile's claude-code and chrome-devtools-mcp and ``install.sh``'s own Chrome
    for Testing pin, so a precheck that runs before the install computes the same
    series the run will. A pin that cannot be read is ``unknown``.
    """
    base = paths.HARNESS_ROOT if root is None else root
    found: dict[str, str] = {key: _UNKNOWN_VERSION for key in _TOOLCHAIN_KEYS}
    runner = base / "scripts" / "ux_runner"
    with contextlib.suppress(OSError, ValueError):
        lock = json.loads((runner / "package-lock.json").read_text(encoding="utf-8"))
        packages = lock.get("packages") if isinstance(lock, dict) else None
        for key, name in (
            ("claude_code", "@anthropic-ai/claude-code"),
            ("chrome_devtools_mcp", "chrome-devtools-mcp"),
        ):
            entry = packages.get(f"node_modules/{name}") if packages else None
            version = entry.get("version") if isinstance(entry, dict) else None
            if isinstance(version, str) and version:
                found[key] = version
    with contextlib.suppress(OSError):
        pin = _CFT_PIN_RE.search((runner / "install.sh").read_text(encoding="utf-8"))
        if pin is not None:
            found["chrome"] = pin.group(1)
    return found


def series_inputs(
    *,
    model: str,
    effort: str,
    prompt_revision: int,
    versions: Mapping[str, str],
    texts: story_prompt.PromptTexts,
) -> stories.SeriesInputs:
    """What makes two nights' flows like for like, beside the story and persona."""
    return stories.SeriesInputs(
        model=model,
        effort=effort,
        prompt_revision=prompt_revision,
        judgement_revision=story_verdict.JUDGEMENT_REVISION,
        relay_revision=story_relay.RELAY_REVISION,
        claude_code=versions.get("claude_code") or _UNKNOWN_VERSION,
        chrome_devtools_mcp=versions.get("chrome_devtools_mcp") or _UNKNOWN_VERSION,
        chrome=versions.get("chrome") or _UNKNOWN_VERSION,
        brief_sha256=texts.brief_sha256,
        ci_contract_sha256=texts.ci_contract_sha256,
    )


def harness_revision(
    environ: Mapping[str, str], root: Path | None = None
) -> tuple[str, bool | None]:
    """The harness commit and whether a harness path differs from it.

    From git when ``root`` is the top of a work tree, with dirty meaning any of
    :data:`HARNESS_PATHS` differs from that commit; otherwise from
    ``UX_HARNESS_SHA`` (a CI job's extraction has no git of its own); otherwise
    ``unknown``, with dirty unknown too.
    """
    base = paths.HARNESS_ROOT if root is None else root
    try:
        top = _git(base, "rev-parse", "--show-toplevel").strip()
        if Path(top).resolve() == base.resolve():
            sha = _git(base, "rev-parse", "--verify", "HEAD^{commit}").strip()
            if _SHA_RE.fullmatch(sha):
                changed = _git(
                    base,
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                    "--",
                    *HARNESS_PATHS,
                )
                return sha, bool(changed.strip())
    except (ui_change.UiChangeError, OSError):
        pass
    named = environ.get("UX_HARNESS_SHA", "").strip()
    if _HARNESS_SHA_RE.fullmatch(named):
        sha, _, dirty = named.partition("+")
        return sha, bool(dirty)
    return "unknown", None


def journal_harness(sha: str, dirty: bool | None) -> str:
    """The journal's ``harness``: the commit, ``+dirty`` when it differs, or unknown."""
    if not _SHA_RE.fullmatch(sha):
        return "unknown"
    return f"{sha}+dirty" if dirty else sha


def index_docs(*, repo_dir: Path, ref: str | None, work_dir: Path) -> DocsCorpus:
    """Index the published docs: a commit's from git objects, or a working tree's.

    A commit's ``docs/**.md`` blobs are written under ``work_dir`` beside the
    harness's own ``docs-site/setup.sh`` (``docs_index.snapshot_from_git``), so a
    symlink in a checkout cannot pull another file into the index; every CI mode
    passes a commit. A working tree is read where it is, and counts as dirty when
    git says its ``docs`` differ from ``HEAD`` or cannot say.
    """
    try:
        if ref is None:
            try:
                dirty = bool(_git(repo_dir, "status", "--porcelain", "--", "docs"))
            except (ui_change.UiChangeError, OSError):
                dirty = True
            source = docs_index.DocsSource(kind="worktree", ref=None, dirty=dirty)
            return DocsCorpus(docs_index.build(repo_dir, source=source), "worktree")
        sha = _git(repo_dir, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
        if not _SHA_RE.fullmatch(sha):
            raise StoryRunError(f"git did not name a commit for the docs ref {ref}")
        tree = _git(repo_dir, "rev-parse", "--verify", f"{sha}:docs").strip()
        snapshot = docs_index.snapshot_from_git(
            repo_dir,
            sha,
            work_dir / "docs",
            setup_sh=paths.HARNESS_ROOT / "docs-site" / "setup.sh",
            git=_git,
        )
        source = docs_index.DocsSource(kind="git", ref=snapshot.ref, dirty=None)
        index = docs_index.build(snapshot.root, source=source)
        return DocsCorpus(index, tree if _SHA_RE.fullmatch(tree) else "worktree")
    except (docs_index.DocsIndexError, ui_change.UiChangeError) as error:
        raise StoryRunError(f"the documentation cannot be indexed: {error}") from None


def compose_prompt(
    *,
    story: stories.Story,
    persona: stories.Persona,
    budget: int,
    texts: story_prompt.PromptTexts,
) -> str:
    """The flow's prompt, ``story_prompt.build_story_prompt``'s, the same in every mode.

    Its only inputs are the catalogue's story and persona, the action budget and
    the two skill texts, so nothing of a merge request and nothing a page showed
    can reach it. One it cannot carry is a :class:`StoryRunError`.
    """
    try:
        return story_prompt.build_story_prompt(
            story=story, persona=persona, budget=budget, texts=texts
        )
    except (story_prompt.StoryPromptError, TypeError) as error:
        raise StoryRunError(f"the story prompt cannot be built: {error}") from None


def collect_story(
    repo_dir: Path, iid: int, target_branch: str, *, remote: str
) -> Mapping[str, Any]:
    """A merge request's StoryInputs v1 from git, as ``story_inputs.collect`` writes it.

    The record, not the object, so a collected MR is read back through
    :meth:`StoryInputs.from_json` exactly as an ``inputs.json`` from elsewhere is.
    """
    return story_inputs.collect(repo_dir, iid, target_branch, remote=remote).to_json()


def run_tui(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float
) -> processes.Completed:
    """Run the Claude TUI on this process's terminal until it exits or ``timeout``.

    The TUI must own the terminal, so it is started in this process's own process
    group with the inherited standard streams, and this process's SIGINT is set to
    a handler that does nothing while it waits: the person's Ctrl-C belongs to the
    TUI, and a handler, unlike an ignored signal, is not inherited across
    ``exec``. Past ``timeout`` the TUI is sent SIGTERM and, ten seconds later,
    SIGKILL, and the result says it timed out.
    """
    started = time.monotonic()
    previous = signal.signal(signal.SIGINT, lambda signum, frame: None)
    try:
        popen = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            list(argv), cwd=cwd, env=dict(env)
        )
        timed_out = False
        try:
            popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            popen.terminate()
            try:
                popen.wait(timeout=10)
            except subprocess.TimeoutExpired:
                popen.kill()
                popen.wait()
        except BaseException:
            popen.kill()
            popen.wait()
            raise
    finally:
        signal.signal(signal.SIGINT, previous)
    return processes.Completed(
        returncode=popen.returncode if popen.returncode is not None else -9,
        stdout="",
        stderr="",
        timed_out=timed_out,
        elapsed=time.monotonic() - started,
    )


def _default_forge(args: argparse.Namespace, credential: str) -> gitlab_api.UxGitlab:
    """The real GitLab client for ``--api-url`` and ``--project``."""
    return gitlab_api.UxGitlab(args.api_url, args.project, credential)


def _scheduled(environ: Mapping[str, str]) -> bool:
    return environ.get("CI_PIPELINE_SOURCE", "").strip() == "schedule"


def nightly_history(
    args: argparse.Namespace,
    environ: Mapping[str, str],
    *,
    forge: Callable[[argparse.Namespace, str], gitlab_api.UxGitlab] = _default_forge,
    clock: Callable[[], float] = time.monotonic,
) -> story_digest.History | None:
    """The earlier scheduled nights a digest is compared with, or None.

    Read only in a scheduled pipeline, with ``GITLAB_REVIEW_TOKEN`` and only by
    GET requests (``gitlab_api.scheduled_digests``): :data:`HISTORY_REF`'s newest
    scheduled pipelines but this one, and in each the :data:`DIGEST_JOB`'s
    :data:`DIGEST_ARTIFACT`, read by job id up to ``story_digest.DIGEST_MAX_BYTES``
    within :data:`HISTORY_SECONDS`. ``story_digest`` validates each one, so a night
    that is missing or invalid is a gap. None when the nights cannot be listed. One
    printed line says what was read or why nothing was, and nothing here fails the
    digest.
    """
    scrub = artifacts.Scrub(artifacts.redactor_for(environ))
    credential = environ.get(review.GITLAB.credential_env, "").strip()
    if not _scheduled(environ):
        reason = "this is not a scheduled pipeline"
    elif not credential:
        reason = f"{review.GITLAB.credential_env} is not set"
    elif not args.api_url or not args.project:
        reason = "the GitLab API URL or the project is not known"
    else:
        reason = ""
    if reason:
        _say(scrub, f"history: not read, because {reason}")
        return None
    try:
        pairs = gitlab_api.scheduled_digests(
            forge(args, credential),
            ref=HISTORY_REF,
            job=DIGEST_JOB,
            artifact=DIGEST_ARTIFACT,
            max_bytes=story_digest.DIGEST_MAX_BYTES,
            deadline=clock() + HISTORY_SECONDS,
            own_pipeline=_pipeline_id(environ),
            clock=clock,
        )
    except (review.Skip, review.Failure) as error:
        _say(scrub, f"history: not read: {error}")
        return None
    except Exception as error:
        _say(scrub, f"history: not read: {_unexpected(scrub, error)}")
        return None
    history = story_digest.History.from_digests(
        [
            (pipeline_id, None if raw is None else story_digest.read_digest(raw))
            for pipeline_id, raw in pairs
        ]
    )
    _say(
        scrub,
        f"history: {len(history.nights)} earlier night(s), {len(history.gaps)} gap(s)",
    )
    return history


class _RedirectRefused(urllib.request.HTTPRedirectHandler):
    """A redirect answers a post as the error it is, and is never followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_SLACK_OPENER = urllib.request.build_opener(_RedirectRefused)


def _usable_webhook(url: str) -> bool:
    """Whether ``url`` is an https URL with a host and no user, password or fragment."""
    if (
        not url
        or len(url) > _WEBHOOK_CHARS
        or not url.isascii()
        or not url.isprintable()
        or " " in url
    ):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and not parts.fragment
        and port != 0
    )


def post_slack(
    webhook: str,
    payload: Mapping[str, str],
    *,
    timeout: float = SLACK_SECONDS,
    opener: urllib.request.OpenerDirector | None = None,
) -> str:
    """POST ``payload`` to a Slack workflow webhook, and say what happened.

    Only a usable https URL is posted to, a redirect is an error rather than a
    new target, at most :data:`_SLACK_ANSWER_BYTES` of the answer is read, and
    nothing raises, because a notification never decides the digest's outcome.
    The webhook is a bearer credential, so no outcome quotes it or anything an
    error says about it.
    """
    if not _usable_webhook(webhook):
        return f"not posted, because {SLACK_WEBHOOK_ENV} is not an https URL"
    body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
    try:
        request = urllib.request.Request(
            webhook,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with (opener or _SLACK_OPENER).open(request, timeout=timeout) as response:
            response.read(_SLACK_ANSWER_BYTES)
            status = response.status
    except urllib.error.HTTPError as error:
        with contextlib.suppress(Exception):
            error.close()
        return f"not posted: Slack answered {error.code}"
    except Exception as error:
        return f"not posted ({type(error).__name__})"
    if 200 <= status < 300:
        return f"posted ({status})"
    return f"not posted: Slack answered {status}"


def post_digest_summary(
    digest: Mapping[str, Any], runtime: Runtime, scrub: artifacts.Scrub
) -> None:
    """The scheduled digest's one-line Slack summary, when a webhook is set.

    ``story_digest.slack_payload`` builds it from closed values only, and
    ``CI_JOB_URL`` is its link. Without :data:`SLACK_WEBHOOK_ENV` nothing is
    posted or printed; outside a scheduled pipeline one line says it was not
    posted; and whatever happens, the digest's own outcome does not change.
    """
    webhook = runtime.environ.get(SLACK_WEBHOOK_ENV, "").strip()
    if not webhook or runtime.slack is None:
        return
    scrub.redactor.add(webhook)
    if not _scheduled(runtime.environ):
        _say(scrub, "slack: not posted, because this is not a scheduled pipeline")
        return
    try:
        payload = story_digest.slack_payload(
            digest, job_url=runtime.environ.get("CI_JOB_URL", "").strip()
        )
        outcome = runtime.slack(webhook, payload)
    except Exception as error:
        outcome = f"not posted ({type(error).__name__})"
    _say(scrub, f"slack: {outcome}")


def default_runtime() -> Runtime:
    """The runtime a run uses: every collaborator is the real one."""
    return Runtime(
        forge=_default_forge,
        collect_story=collect_story,
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
        run_tui=run_tui,
        render_video=recording.render_video,
        chapters=recording.chapters,
        index_docs=index_docs,
        compose_prompt=compose_prompt,
        harness=harness_revision,
        history=nightly_history,
        slack=post_slack,
    )


@dataclass(frozen=True)
class Plan:
    """What a run is to do, decided from the catalogue, git and the API alone.

    ``ui_kind`` is ``build`` (``build_dir`` served by the proxy) or ``deployed``
    (the stack's own ``web_url``). ``docs_ref`` is the commit the docs are read
    from, or None for the working tree. ``head_sha`` heads the reply: the MR head,
    the nightly's commit, or the harness commit.
    """

    mode: str
    keys: tuple[stories.FlowKey, ...]
    stack: str
    model: str
    effort: str
    ui_kind: str
    build_dir: Path | None
    web_url: str | None
    src_ui_tree: str | None
    docs_ref: str | None
    head_sha: str
    inputs: StoryInputs | None = None
    selection: changelog_select.Selection | None = None
    change: ui_change.UiChange | None = None
    stack_state: str | None = None
    replayed: str | None = None


@dataclass
class Decision:
    """What the precheck concluded. ``status`` is proceed, skipped or unchanged.

    ``reply`` is the one-line story reply a merge request gets for an unchanged
    head or a used-up run allowance, posted only with ``--post``; ``extra`` is
    what ``precheck.json`` adds, such as the CHANGELOG leads no story covers.
    """

    proceed: bool
    status: str
    detail: str
    plan: Plan | None = None
    reply: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def catalogue_path() -> Path:
    """The catalogue in the harness tree."""
    return paths.HARNESS_ROOT / "scripts" / "ux_stories.toml"


def load_catalogue() -> stories.Catalogue:
    """The harness's catalogue, or a :class:`StoryRunError` naming what is wrong."""
    try:
        return stories.load_catalogue(catalogue_path())
    except stories.CatalogueError as error:
        raise StoryRunError(f"the story catalogue cannot be used: {error}") from None


def catalogue_sha256() -> str:
    """The sha256 of the catalogue's bytes, which ``story-run.json`` records."""
    try:
        return hashlib.sha256(catalogue_path().read_bytes()).hexdigest()
    except OSError as error:
        raise StoryRunError(f"the story catalogue cannot be read: {error}") from None


def _no_token_message() -> str:
    return (
        f"{review.GITLAB.credential_env} is not set, so the stories can be neither "
        "checked against the MR's notes nor posted; run without --post for a dry "
        "run"
    )


def _forge(
    args: argparse.Namespace, runtime: Runtime, credential: str
) -> gitlab_api.UxGitlab:
    """The GitLab client, or a skip naming whichever of its two settings is missing."""
    if not args.api_url:
        raise review.Skip(
            "no GitLab API URL: pass --api-url or set CI_API_V4_URL, as GitLab CI does"
        )
    if not args.project:
        raise review.Skip("no project: pass --project or run inside GitLab CI")
    return runtime.forge(args, credential)


def _ui_env(environ: Mapping[str, str]) -> dict[str, str]:
    """``UX_TEST_UI_ENV`` decoded, or ``{}`` when it is absent or cannot be read.

    Only for what a missing value merely leaves out (the web URL line); every
    check that needs the variable decodes it itself and says what is wrong.
    """
    text = environ.get("UX_TEST_UI_ENV", "")
    if not text.strip():
        return {}
    try:
        return egress.ui_env_from_b64(text)
    except ux_test_proxy.EgressConfigError:
        return {}


def _no_web_url(environ: Mapping[str, str], problem: str) -> str:
    """Why no web URL is known: ``UX_TEST_UI_ENV`` missing, else ``problem``."""
    if not environ.get("UX_TEST_UI_ENV", "").strip():
        return _NO_UI_ENV
    return problem


def _web_url(args: argparse.Namespace, environ: Mapping[str, str]) -> str | None:
    """The deployed stack's web URL: ``--web-url``, else the UI env's line, else None."""
    named = (args.web_url or "").strip()
    if named:
        return named
    line = _ui_env(environ).get(WEB_URL_KEY, "").strip()
    return line or None


def _groups(environ: Mapping[str, str]) -> frozenset[str]:
    """The Cognito groups ``UX_TEST_PERSONAS`` can sign in as; empty when unreadable."""
    try:
        return frozenset(signin.personas_from_env(environ.get("UX_TEST_PERSONAS", "")))
    except signin.SignInError:
        return frozenset()


def settings_problem(
    args: argparse.Namespace,
    environ: Mapping[str, str],
    *,
    stack: str,
    web_url: str | None,
    groups: Iterable[str],
    sign_in: bool = True,
) -> str:
    """Why the CI variables cannot drive the planned flows, or ``""`` when they can.

    Each value is decoded and checked before anything is installed or started,
    the web URL with the egress rules it joins, and no message carries a value.
    ``groups`` are the Cognito groups the planned flows sign in as; at least one
    must have an entry in ``UX_TEST_PERSONAS`` (a flow whose group has none is
    ``not_run (persona)``). Without ``sign_in`` (a watched run, whose browser the
    container has signed in) the personas are not needed.
    """
    ui_text = environ.get("UX_TEST_UI_ENV", "")
    if not ui_text.strip():
        return _NO_UI_ENV
    try:
        ux_test_proxy.stack_rules(
            egress.ui_env_from_b64(ui_text), stack, web_url=web_url
        )
    except ux_test_proxy.EgressConfigError as error:
        return f"UX_TEST_UI_ENV cannot be used: {error}"
    if sign_in:
        persona_text = environ.get("UX_TEST_PERSONAS", "")
        if not persona_text.strip():
            return "UX_TEST_PERSONAS is not set, so no persona can be signed in"
        try:
            personas = signin.personas_from_env(persona_text)
        except signin.SignInError as error:
            return str(error)
        wanted = sorted(set(groups))
        if wanted and not any(group in personas for group in wanted):
            return (
                "UX_TEST_PERSONAS holds none of the groups these flows sign in as: "
                + ", ".join(wanted)
            )
    expected = environ.get("UX_TEST_AWS_ACCOUNT_ID", "").strip()
    if not expected:
        return (
            "UX_TEST_AWS_ACCOUNT_ID is not set, so the run cannot check which "
            "account its credentials reach"
        )
    if not _ACCOUNT_ID_RE.fullmatch(expected):
        return "UX_TEST_AWS_ACCOUNT_ID is not a 12-digit account id"
    return ""


def parse_shard(text: str) -> tuple[int, int]:
    """``i/n`` as ``(i, n)``, 1-based, with ``1 <= i <= n``; ValueError otherwise."""
    found = _SHARD_RE.fullmatch(text.strip())
    if found is None:
        raise ValueError("--shard is i/n, such as 2/5")
    index, total = int(found.group(1)), int(found.group(2))
    if index > total:
        raise ValueError("--shard i/n needs i no greater than n")
    return index, total


def _shard(keys: Sequence[stories.FlowKey], spec: str | None) -> list[stories.FlowKey]:
    if spec is None:
        return sorted(keys)
    index, total = parse_shard(spec)
    return stories.shard(keys, index, total)


def _read_bounded(path: Path, limit: int, what: str) -> bytes:
    """A regular file, opened without following a link, of at most ``limit`` bytes."""
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
        )
    except OSError as error:
        raise StoryRunError(
            f"{what} cannot be read: {error.strerror or type(error).__name__}"
        ) from None
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise StoryRunError(f"{what} is not a regular file")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise StoryRunError(f"{what} is larger than {limit} bytes")
    return data


def _read_json(path: Path, what: str, limit: int = _RECORD_MAX_BYTES) -> Any:
    try:
        return json.loads(_read_bounded(path, limit, what).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise StoryRunError(f"{what} is not valid JSON") from None


def _is_whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def deploy_state(path: Path | None) -> str | None:
    """The review stack's state from ``deployment_summary.txt``; None without a path.

    A summary that is absent or cannot be read is ``missing``, which runs the
    flows and records the night as not comparable.
    """
    if path is None:
        return None
    try:
        text = _read_bounded(path, _SUMMARY_MAX_BYTES, "the deployment summary")
    except StoryRunError:
        return "missing"
    return story_digest.classify_stack_state(text.decode("utf-8", errors="replace"))


def _flow_keys(
    args: argparse.Namespace, catalogue: stories.Catalogue
) -> list[stories.FlowKey]:
    """The flows ``--story``/``--as`` or ``--all``/``--tier`` name, before sharding."""
    if args.all:
        tiers = stories.TIERS if args.tier == "all" else (args.tier,)
        return stories.flow_keys(catalogue, tiers=tiers)
    try:
        story = catalogue.story(args.story)
    except KeyError:
        raise StoryRunError(f"the catalogue has no story {args.story!r}") from None
    persona = getattr(args, "as_persona", None)
    if persona is not None and persona not in story.personas:
        raise StoryRunError(
            f"{story.id} runs as {', '.join(story.personas)}, not {persona!r}"
        )
    return stories.flow_keys(
        catalogue, tiers=stories.TIERS, story=story.id, persona=persona
    )


def _local_head(runtime: Runtime) -> str:
    """What heads a run's reply outside a merge request: the commit under test."""
    commit = runtime.environ.get("CI_COMMIT_SHA", "").strip()
    if _SHA_RE.fullmatch(commit):
        return commit
    sha, _ = runtime.harness(runtime.environ)
    return sha if _SHA_RE.fullmatch(sha) else "0" * 40


def _mode(args: argparse.Namespace, environ: Mapping[str, str]) -> str:
    if args.mr is not None:
        return "mr"
    if args.interactive:
        return "watch"
    if args.all and _ci_variable(environ):
        return "nightly"
    return "local"


def _ui_source(
    args: argparse.Namespace, environ: Mapping[str, str]
) -> tuple[str, Path | None, str | None, str]:
    """``(kind, build_dir, web_url, problem)`` for a run outside a merge request.

    A ``--build-dir`` is served; otherwise the deployed stack's web URL is
    required, from ``--web-url`` or the UI env's line.
    """
    if args.build_dir is not None:
        build = Path(args.build_dir)
        if not (build / "index.html").is_file():
            return "build", build, None, f"no UI build: {build} has no index.html"
        return "build", build, None, ""
    url = _web_url(args, environ)
    if url is None:
        return (
            "deployed",
            None,
            None,
            _no_web_url(
                environ,
                "the deployed UI's web URL is not known: pass --web-url, or add a "
                f"{WEB_URL_KEY}=<the stack's ApplicationWebURL> line to UX_TEST_UI_ENV",
            ),
        )
    return "deployed", None, url, ""


def precheck_flows(
    args: argparse.Namespace, runtime: Runtime, catalogue: stories.Catalogue
) -> Decision:
    """The decision for ``--story``, ``--all`` and ``--interactive`` runs.

    In order: the flows (after ``--shard``), the review stack's state when a
    deployment summary is given (a stack still updating skips the shard before
    anything is installed), the UI source and the CI variables.
    """
    environ = runtime.environ
    mode = _mode(args, environ)
    keys = _shard(_flow_keys(args, catalogue), args.shard)
    if not keys:
        where = f" in shard {args.shard}" if args.shard else ""
        return Decision(False, "skipped", f"no flow to run{where}")
    state = deploy_state(Path(args.deploy_summary) if args.deploy_summary else None)
    if state == "running":
        return Decision(
            False,
            "skipped",
            "the review stack was still updating when the deployment ended, so the "
            "night's flows would test a stack that is changing",
        )
    kind, build, url, problem = _ui_source(args, environ)
    if problem and mode != "watch":
        return Decision(False, "skipped", problem)
    groups = [catalogue.persona(key.persona).group for key in keys]
    problem = settings_problem(
        args,
        environ,
        stack=args.stack,
        web_url=url,
        groups=groups,
        sign_in=mode != "watch",
    )
    if problem:
        return Decision(False, "skipped", problem)
    docs_ref = args.docs
    if docs_ref is None and mode == "nightly":
        commit = environ.get("CI_COMMIT_SHA", "").strip()
        docs_ref = commit if _SHA_RE.fullmatch(commit) else None
    plan = Plan(
        mode=mode,
        keys=tuple(keys),
        stack=args.stack,
        model=args.model,
        effort=args.effort,
        ui_kind=kind,
        build_dir=build,
        web_url=url,
        src_ui_tree=None,
        docs_ref=docs_ref,
        head_sha=_local_head(runtime),
        stack_state=state if mode == "nightly" else None,
    )
    return Decision(True, "proceed", f"{len(keys)} flow(s)", plan=plan)


def precheck_replay(
    args: argparse.Namespace, runtime: Runtime, catalogue: stories.Catalogue
) -> Decision:
    """The decision for ``--replay``: the earlier run's flows, model, docs and UI.

    The stories and personas must still be in this harness's catalogue, which is
    why the Docker runner mounts the run's own harness commit; one that differs
    from the run's is said in a ``WARNING:`` line, and so is docs read from a
    working tree, which cannot be read again as they were. The stack is
    ``--stack`` when it is given, which is what the Docker runner's warning about
    a replay against another stack says will happen, and else the run's own.
    """
    environ = runtime.environ
    folder = Path(args.replay)
    record = _read_json(folder / story_verdict.RUN_FILE, "the replayed story-run.json")
    if isinstance(record, dict) and record.get("mode") == "pair":
        raise StoryRunError(
            "a pair run was played by a session its developer started, so it has no "
            "model or effort to replay; run its story unattended instead"
        )
    try:
        contexts = story_verdict.run_record(record)
    except story_verdict.RunRecordError as error:
        raise StoryRunError(f"the replayed run cannot be read: {error}") from None
    keys = sorted({stories.FlowKey.parse(c.key) for c in contexts if c.attempt == 1})
    for key in keys:
        try:
            story = catalogue.story(key.story)
        except KeyError:
            raise StoryRunError(
                f"this harness's catalogue has no story {key.story!r}; replay a run "
                "with the harness it ran on"
            ) from None
        if key.persona not in story.personas:
            raise StoryRunError(
                f"this harness's {key.story} does not run as {key.persona}"
            )
    keys = _shard(keys, args.shard)
    if not keys:
        return Decision(False, "skipped", "the replayed run planned no flow")
    model = record.get("model") if isinstance(record.get("model"), str) else None
    effort = record.get("effort") if isinstance(record.get("effort"), str) else None
    if effort not in agent.EFFORT_LEVELS:
        effort = None
    stack_record = record.get("stack") if isinstance(record.get("stack"), dict) else {}
    stack = (
        stack_record.get("name") if isinstance(stack_record.get("name"), str) else None
    )
    docs = record.get("docs") if isinstance(record.get("docs"), dict) else {}
    ref = docs.get("ref")
    docs_ref = ref if isinstance(ref, str) and _SHA_RE.fullmatch(ref) else None
    warnings: list[str] = []
    if docs_ref is None:
        warnings.append(
            "the replayed run read the docs of a working tree, which cannot be read "
            "again as they were; this replay reads the working tree as it is now"
        )
    harness = record.get("harness_sha")
    here, _ = runtime.harness(environ)
    if isinstance(harness, str) and harness != here:
        warnings.append(
            f"this harness is {here[:12]}, the replayed run's was {harness[:12]}; "
            "mount the run's own harness to replay it exactly"
        )
    source = (
        record.get("ui_source") if isinstance(record.get("ui_source"), dict) else {}
    )
    kind = "build" if source.get("kind") == "build" else "deployed"
    build: Path | None = None
    url: str | None = None
    if kind == "build":
        if (
            args.build_dir is None
            or not (Path(args.build_dir) / "index.html").is_file()
        ):
            return Decision(
                False,
                "skipped",
                "the replayed run tested an MR's UI build; pass --build-dir with that "
                "build",
            )
        build = Path(args.build_dir)
    else:
        url = _web_url(args, environ)
        if url is None:
            return Decision(
                False,
                "skipped",
                "the replayed run tested the deployed UI, whose web URL is not known: "
                "pass --web-url",
            )
    run_stack = args.stack or stack or session.REVIEW_STACK
    groups = [catalogue.persona(key.persona).group for key in keys]
    problem = settings_problem(
        args, environ, stack=run_stack, web_url=url, groups=groups
    )
    if problem:
        return Decision(False, "skipped", problem)
    tree = source.get("src_ui_tree")
    plan = Plan(
        mode="local",
        keys=tuple(keys),
        stack=run_stack,
        model=model or args.model,
        effort=effort or args.effort,
        ui_kind=kind,
        build_dir=build,
        web_url=url,
        src_ui_tree=tree if isinstance(tree, str) and _SHA_RE.fullmatch(tree) else None,
        docs_ref=docs_ref,
        head_sha=_local_head(runtime),
        replayed=str(folder),
    )
    return Decision(
        True,
        "proceed",
        f"{len(keys)} flow(s) replayed",
        plan=plan,
        extra={"warnings": warnings},
    )


def _with_metadata(
    inputs: StoryInputs, merge_request: review.MergeRequest
) -> StoryInputs:
    """``inputs`` with the API's title, author, URL and source branch, when given."""
    record = inputs.mr
    title = merge_request.title[: mr_inputs.TITLE_CHARS]
    return dataclasses.replace(
        inputs,
        mr=dataclasses.replace(
            record,
            title=title or record.title,
            author=merge_request.author or record.author,
            web_url=merge_request.web_url or record.web_url,
            source_branch=merge_request.source_branch or record.source_branch,
            title_from_git=record.title_from_git and not title,
        ),
    )


def written_inputs(inputs: StoryInputs, scrub: artifacts.Scrub) -> StoryInputs:
    """``inputs`` as ``inputs.json`` holds them: prose and CHANGELOG redacted, the rest exact.

    The precheck selects from this form as the run reads it back, so the two
    cannot select differently. The SHAs, the trees and the changed paths stay
    exact, because the run classifies and labels from them.
    """
    record = inputs.mr.to_json()
    for name in _INPUTS_PROSE_FIELDS:
        record[name] = scrub(record[name])
    return dataclasses.replace(
        inputs,
        mr=mr_inputs.MrInputs.from_json(record),
        changelog_base=scrub(inputs.changelog_base),
        changelog_head=scrub(inputs.changelog_head),
    )


def write_inputs(inputs: StoryInputs, path: Path, scrub: artifacts.Scrub) -> Path:
    """Write ``inputs.json`` in its written form (:func:`written_inputs`).

    A record larger than ``story_inputs.INPUTS_MAX_BYTES``, the most a run reads
    back, is refused rather than written, so a precheck fails before the install
    instead of leaving the run a file it cannot read.
    """
    shown = written_inputs(inputs, scrub)
    scrub.keep(shown.mr.head_sha, shown.mr.base_sha)
    scrub.keep(*(tree for tree in (shown.docs_tree, shown.src_ui_tree) if tree))
    text = json.dumps(shown.to_json(), indent=2, ensure_ascii=False) + "\n"
    size = len(text.encode("utf-8"))
    if size > story_inputs.INPUTS_MAX_BYTES:
        raise StoryRunError(
            f"inputs.json would be {size} bytes, more than the "
            f"{story_inputs.INPUTS_MAX_BYTES} a run reads back"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    artifacts.write_artifact(path, text)
    return path


def story_inputs_for(
    args: argparse.Namespace, runtime: Runtime, scrub: artifacts.Scrub
) -> StoryInputs:
    """The MR's StoryInputs, read from ``--inputs`` or collected from git.

    A record for another MR than ``--mr`` is refused: it would label one MR's
    stories with another's head.
    """
    try:
        if args.inputs is not None:
            data = _read_json(
                Path(args.inputs), "inputs.json", story_inputs.INPUTS_MAX_BYTES
            )
        else:
            data = runtime.collect_story(
                Path(args.repo_dir), args.mr, args.target_branch, remote=args.remote
            )
        inputs = written_inputs(StoryInputs.from_json(data), scrub)
    except mr_inputs.InputsError as error:
        raise StoryRunError(f"the MR's story inputs cannot be read: {error}") from None
    if inputs.mr.iid != args.mr:
        raise StoryRunError(f"the inputs describe MR !{inputs.mr.iid}, not !{args.mr}")
    return inputs


def _selection_summary(selection: changelog_select.Selection) -> dict[str, Any]:
    return {
        "stories": list(selection.stories),
        "flows": [str(key) for key in selection.flows],
        "dropped_personas": list(selection.dropped_personas),
        "warnings": list(selection.warnings),
        "scores": [
            {
                "story": score.story,
                "changelog_points": score.changelog_points,
                "secondary_points": score.secondary_points,
                "reasons": list(score.reasons),
            }
            for score in selection.scores
        ],
    }


def _docs_changed(inputs: StoryInputs) -> bool:
    return any(
        docs_index.DOC_PATH_RE.fullmatch(path)
        for changed in inputs.mr.changed
        for path in (changed.path, changed.old_path or "")
    )


def plan_series(
    catalogue: stories.Catalogue,
    keys: Iterable[stories.FlowKey],
    inputs: stories.SeriesInputs,
) -> list[tuple[stories.FlowKey, str]]:
    """Each flow key with its series hash."""
    return [
        (
            key,
            stories.series_hash(
                stories.story_hash(catalogue, catalogue.story(key.story)),
                key.persona,
                inputs,
            ),
        )
        for key in keys
    ]


def sel_sources(
    *, ui_kind: str, src_ui_tree: str | None, docs_tree: str | None
) -> tuple[str, str]:
    """The UI source and the docs tree a ``sel`` is computed over.

    A build is named by its ``src/ui`` tree (all zeros for a local build that has
    none) and the deployed UI as ``deployed``; docs from a commit by their tree id
    and a working tree's as ``worktree``.
    """
    if ui_kind == "build":
        ui_source = f"build:{src_ui_tree if src_ui_tree else '0' * 40}"
    else:
        ui_source = "deployed"
    tree = docs_tree if docs_tree and _SHA_RE.fullmatch(docs_tree) else "worktree"
    return ui_source, tree


def selection_sel(
    pairs: Sequence[tuple[stories.FlowKey, str]],
    *,
    ui_kind: str,
    src_ui_tree: str | None,
    docs_tree: str | None,
) -> str:
    """The reply's ``sel``: the flows and their series, the UI and the docs."""
    ui_source, tree = sel_sources(
        ui_kind=ui_kind, src_ui_tree=src_ui_tree, docs_tree=docs_tree
    )
    return changelog_select.selection_hash(pairs, ui_source=ui_source, docs_tree=tree)


def precheck_mr(
    args: argparse.Namespace,
    runtime: Runtime,
    catalogue: stories.Catalogue,
    scrub: artifacts.Scrub,
) -> Decision:
    """The decision for ``--mr``, from git, the catalogue and the API alone.

    In order: the inputs; with a token, the MR itself (a draft is not run, and a
    head that has moved on is left to the newer head's pipeline); the stories the
    added CHANGELOG entries select, where most MRs stop; which UI the run tests
    (the MR's build when it changes ``src/ui``, else the deployed UI when it
    changes ``docs/``, else nothing); the CI variables; and, with a token, the
    token's own story replies: a head already replied to is unchanged, an MR that
    has used ``mr_runs`` gets one limit reply, and a head whose stories, UI and
    docs are those of the latest run gets the unchanged reply. An MR's runs are
    its posted run replies or, when more, its earlier story jobs whose precheck
    went ahead (``gitlab_api.runs_started``), so a run cancelled by a newer push,
    stopped at the job's timeout or never posted still counts. ``sel`` is
    computed with the harness's pinned versions, which are the ones the install
    records.
    """
    environ = runtime.environ
    credential = environ.get(review.GITLAB.credential_env, "").strip()
    inputs = story_inputs_for(args, runtime, scrub)
    forge: gitlab_api.UxGitlab | None = None
    if credential:
        try:
            forge = _forge(args, runtime, credential)
            merge_request = forge.merge_request(args.mr)
        except review.Skip as skip:
            return Decision(False, "skipped", str(skip))
        if merge_request.draft or merge_request.title.startswith("Draft:"):
            return Decision(False, "skipped", "a draft; drafts are not run")
        moved = merge_request.head_sha
        if moved and moved != inputs.mr.head_sha:
            return Decision(
                False,
                "skipped",
                f"the MR head moved to {moved[:8]} after {inputs.mr.head_sha[:8]} was "
                "collected; a pipeline for the new head runs it",
            )
        inputs = written_inputs(_with_metadata(inputs, merge_request), scrub)
    head = inputs.mr.head_sha
    change = ui_change.classify(inputs.mr.changed)
    added = changelog_select.added_entries(inputs.changelog_base, inputs.changelog_head)
    groups = _groups(environ)
    selection = changelog_select.select(
        catalogue,
        added,
        edited_sections=inputs.docs_sections_edited,
        changed_files=[changed.path for changed in inputs.mr.changed],
        available_groups=groups or stories.SIGN_IN_GROUPS,
    )
    extra: dict[str, Any] = {
        "selection": _selection_summary(selection),
        "uncovered": list(selection.uncovered),
    }
    if not selection.stories:
        return Decision(
            False,
            "skipped",
            "no story covers this MR's CHANGELOG entries",
            extra=extra,
        )
    if not selection.flows:
        return Decision(
            False,
            "skipped",
            "UX_TEST_PERSONAS holds no group the selected stories sign in as",
            extra=extra,
        )
    if change.user_visible:
        kind, build, url = "build", Path(args.build_dir), None
        if not (build / "index.html").is_file():
            return Decision(
                False,
                "skipped",
                "no UI build: ux_test_build was skipped or failed",
                extra=extra,
            )
        built = _pipeline_head(environ)
        if built is not None and built != head:
            return Decision(
                False,
                "skipped",
                f"this pipeline built {built[:8]}, but the MR head is now {head[:8]}; "
                "the newer head's own pipeline runs it",
                extra=extra,
            )
    elif _docs_changed(inputs):
        kind, build, url = "deployed", None, _web_url(args, environ)
        if url is None:
            return Decision(
                False,
                "skipped",
                _no_web_url(
                    environ,
                    "the MR changes the docs only, and the deployed UI's web URL is "
                    f"not known: add a {WEB_URL_KEY}=<the stack's ApplicationWebURL> "
                    "line to UX_TEST_UI_ENV",
                ),
                extra=extra,
            )
    elif change.backend or change.api_contract:
        return Decision(
            False,
            "skipped",
            "only the backend or the API contract changes besides the CHANGELOG; the "
            "nightly run after the merge runs " + ", ".join(selection.stories),
            extra=extra,
        )
    else:
        return Decision(False, "skipped", "nothing user-visible changed", extra=extra)
    problem = settings_problem(
        args,
        environ,
        stack=args.stack,
        web_url=url,
        groups=[catalogue.persona(key.persona).group for key in selection.flows],
    )
    if problem:
        return Decision(False, "skipped", problem, extra=extra)
    pairs = plan_series(
        catalogue,
        selection.flows,
        series_inputs(
            model=args.model,
            effort=args.effort,
            prompt_revision=story_prompt.STORY_PROMPT_REVISION,
            versions=pinned_versions(),
            texts=read_texts(),
        ),
    )
    sel = selection_sel(
        pairs, ui_kind=kind, src_ui_tree=inputs.src_ui_tree, docs_tree=inputs.docs_tree
    )
    extra["sel"] = sel
    plan = Plan(
        mode="mr",
        keys=selection.flows,
        stack=args.stack,
        model=args.model,
        effort=args.effort,
        ui_kind=kind,
        build_dir=build,
        web_url=url,
        src_ui_tree=inputs.src_ui_tree,
        docs_ref=head,
        head_sha=head,
        inputs=inputs,
        selection=selection,
        change=change,
    )
    if forge is None:
        if args.post:
            return Decision(False, "skipped", _no_token_message(), extra=extra)
    else:
        try:
            me = forge.current_user()["id"]
            mine = gitlab_api.my_notes(forge.notes(args.mr), me)
        except review.Skip as skip:
            return Decision(False, "skipped", str(skip), extra=extra)
        if story_section.has_story_reply(mine, head):
            return Decision(
                False, "unchanged", "already run for this head", plan=plan, extra=extra
            )
        try:
            started = gitlab_api.runs_started(
                forge,
                args.mr,
                job=MR_JOB,
                marker=_PROCEEDED_RE,
                own_pipeline=_pipeline_id(environ),
                enough=catalogue.runs.mr_runs,
                deadline=runtime.clock() + RUNS_COUNT_SECONDS,
                clock=runtime.clock,
            )
        except (review.Skip, review.Failure) as unread:
            return Decision(
                False,
                "skipped",
                f"this MR's earlier story runs cannot be counted: {unread}",
                plan=plan,
                extra=extra,
            )
        used = max(story_section.runs_used(mine), started)
        if used >= catalogue.runs.mr_runs:
            limited = any(
                marker.kind == "limit" for marker in story_section.story_markers(mine)
            )
            reply = (
                None
                if limited
                else story_section.compose_limit(
                    head_sha=head, sel=sel, iid=args.mr, runs=used
                )
            )
            return Decision(
                False,
                "skipped",
                f"this MR has used its {used} story runs",
                plan=plan,
                reply=reply,
                extra=extra,
            )
        latest = story_section.latest_run(mine)
        if latest is not None and latest.sel == sel:
            return Decision(
                False,
                "unchanged",
                f"the same stories, UI and docs as {latest.sha[:8]}",
                plan=plan,
                reply=story_section.compose_unchanged(
                    head_sha=head, sel=sel, since_sha=latest.sha
                ),
                extra=extra,
            )
    flows = len(selection.flows)
    return Decision(
        True,
        "proceed",
        f"{flows} flow(s) of {len(selection.stories)} story(ies)",
        plan=plan,
        extra=extra,
    )


def precheck(
    args: argparse.Namespace, runtime: Runtime, scrub: artifacts.Scrub
) -> Decision:
    """Whether the run is worth a browser, and what it is to run.

    It runs on a bare ``python3`` before the toolchain is installed, so a skip
    costs the CI job about a minute. ``StoryRunError`` means the catalogue, a
    file or git could not answer.
    """
    catalogue = load_catalogue()
    if args.mr is not None:
        decision = precheck_mr(args, runtime, catalogue, scrub)
    elif args.replay is not None:
        decision = precheck_replay(args, runtime, catalogue)
    else:
        decision = precheck_flows(args, runtime, catalogue)
    plan = decision.plan
    if decision.proceed and plan is not None and plan.model not in MODEL_RATES:
        return Decision(
            False,
            "failed",
            f"the model {plan.model!r} has no pinned rates in MODEL_RATES, so its "
            "spend could be counted only as the CLI reports it; add its "
            "config_library/pricing.yaml rates there, or run a model that has them",
            extra=decision.extra,
        )
    return decision


@dataclass(frozen=True)
class Flow:
    """One planned flow: the catalogue's story and persona, and its series."""

    key: stories.FlowKey
    story: stories.Story
    persona: stories.Persona
    expectation: str
    story_hash: str
    series: str


@dataclass
class Attempt:
    """One run of one flow, filled in as it goes.

    ``context`` is the flow's record for ``story-run.json``. ``withheld`` is why
    its video may not be rendered (``session.review_stack_problem``), or ``""``.
    ``recording`` is the recorder's session directory while it may still be
    rendered, and ``keep`` the run directory kept around it until then.
    """

    flow: Flow
    number: int
    scale: int
    budget: int
    cap_usd: float
    context: story_verdict.FlowContext
    withheld: str = ""
    opened: bool = False
    refusals: list[dict[str, Any]] = field(default_factory=list)
    recording: Path | None = None
    keep: Path | None = None
    facts: signin.PageFacts | None = None
    result: story_verdict.FlowResult | None = None

    @property
    def folder(self) -> str:
        """The attempt's directory under the artifacts, as its journal path names it."""
        return story_verdict.journal_path(self.context.key, self.number).rsplit("/", 1)[
            0
        ]


@dataclass
class BrowserSteps:
    """A run's browser collaborators, each noting its name before it is called.

    A failure inside ``ConfinedBrowser.open`` is the step that raised, which is
    what names a flow's harness error (``launch``, ``selftest``, ``sign_in``,
    ``viewport``); the confined browser itself is unchanged.
    """

    launch_proxy: Callable[..., Any]
    launch_chrome: Callable[..., Any]
    wait_for_cdp: Callable[..., Any]
    first_page_target: Callable[..., Any]
    open_session: Callable[..., Any]
    verify_policy: Callable[..., Any]
    verify_proxy_path: Callable[..., Any]
    sign_in: Callable[..., Any]
    page_facts: Callable[..., Any]
    verify_viewport: Callable[..., Any]
    measure_viewport: Callable[..., Any]
    socket_watch: Callable[..., Any]
    kill_strays: Callable[..., Any]


def tracked_browser(runtime: Runtime, steps: list[str]) -> BrowserSteps:
    """``runtime``'s browser collaborators, appending each name to ``steps`` first."""

    def wrap(name: str) -> Callable[..., Any]:
        call = getattr(runtime, name)

        def step(*args: Any, **kwargs: Any) -> Any:
            steps.append(name)
            return call(*args, **kwargs)

        return step

    return BrowserSteps(**{name: wrap(name) for name in _BROWSER_STEPS})


#: Exceptions that mean a step of a flow could not be done: the flow is a harness
#: error, and the run goes on with the next flow.
_STEP_FAILURES: tuple[type[Exception], ...] = (
    review.Failure,
    egress.ProxyError,
    chrome.ChromeError,
    signin.SignInError,
    recording.RecorderError,
    agent.AgentError,
    ux_test_proxy.EgressConfigError,
    session.SessionError,
    docs_index.DocsIndexError,
    stories.CatalogueError,
    story_verdict.RunRecordError,
    OSError,
)

#: The story server's directory, as a refusal of one of its outputs names it.
_SERVER = readback.Writer("the story server's", "directory", StoryRunError)


def _bounded_tree(value: object, scrub: artifacts.Scrub) -> object:
    """``value`` with every string leaf redacted and cut to its length before.

    A journal string is already within the bound its reader checks, and a
    placeholder can be longer than the value it replaces, so a redacted leaf is
    never allowed past the original's length.
    """
    if isinstance(value, str):
        shown = scrub(value)
        return shown[: len(value)] if len(shown) > len(value) else shown
    if isinstance(value, dict):
        return {key: _bounded_tree(item, scrub) for key, item in value.items()}
    if isinstance(value, list):
        return [_bounded_tree(item, scrub) for item in value]
    return value


def redacted_journal(raw: bytes, scrub: artifacts.Scrub) -> bytes:
    """The journal as the artifacts keep it: each line's strings redacted in place.

    Lines are redacted as decoded JSON, so a value the server JSON-escaped is
    still found, and written back in the server's own compact form, keys in
    order. A final segment with no newline is a torn write, which the reader
    ignores, so it is left out; a line that is not JSON is redacted as text,
    which the reader still refuses.
    """
    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\n")[:-1]
    out: list[str] = []
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            out.append(scrub(line))
            continue
        out.append(
            json.dumps(
                _bounded_tree(record, scrub), ensure_ascii=False, separators=(",", ":")
            )
        )
    return "".join(line + "\n" for line in out).encode("utf-8")


def _matches_flow(
    journal: story_verdict.Journal, context: story_verdict.FlowContext
) -> bool:
    start = journal.start
    return (
        start.story,
        start.persona,
        start.group,
        start.expectation,
        start.kind,
        start.series,
        start.attempt,
    ) == (
        context.story,
        context.persona,
        context.group,
        context.expectation,
        context.kind,
        context.series,
        context.attempt,
    )


def judge_attempt(
    artifacts_dir: Path, context: story_verdict.FlowContext
) -> story_verdict.FlowResult:
    """One attempt judged from its copied journal, as ``judge_run`` will judge it."""
    journal: story_verdict.Journal | None = None
    if context.journal is not None:
        try:
            read = story_verdict.read_journal(artifacts_dir / context.journal)
        except story_verdict.JournalError:
            read = None
        if read is not None and _matches_flow(read, context):
            journal = read
    verdict = story_verdict.judge(journal, context)
    return story_verdict.summarize(journal, context, verdict)


class FlowSession:
    """One attempt's confined browser, recorder and ``claude`` session.

    Its run directory is made fresh under ``/tmp``, so the attempt gets a fresh
    Chrome profile, proxy directory and agent home, and nothing of an earlier
    flow's survives into it. Each phase leaves on ``self`` what the cleanup must
    stop, so a failure or a cancel at any point stops exactly what was started,
    in the MR UX test's order: the DevTools session, the recorder, Chrome and its
    uid's strays, the socket watch, the proxy; then the outputs are copied and the
    run directory is removed, or kept around a recording that may be rendered.
    """

    def __init__(self, run: StoryRun, attempt: Attempt) -> None:
        self.run = run
        self.attempt = attempt
        self.steps: list[str] = []
        self.browser = session.ConfinedBrowser(
            tracked_browser(run.runtime, self.steps), say=run.say
        )
        self.run_dir: Path | None = None
        self.rec_out: Path | None = None
        self.recorder_env: dict[str, str] = {}
        self.session_dir: Path | None = None
        self.audit: chrome.SocketAudit | None = None
        self.harness_error: str | None = None
        self.not_run_reason: str | None = None
        self.outcome: story_verdict.SessionOutcome | None = None
        self.report: str | None = None
        self.journal_copied: str | None = None
        self.copied = False

    def run_attempt(self) -> None:
        """Open, sign in, decide the video, record, run the session, close."""
        run, attempt = self.run, self.attempt
        flow = attempt.flow
        run_dir = self._make_run_dir()
        plan = run.browser_plan(flow, run_dir)
        try:
            opened = self.browser.open(plan)
        except signin.SignInError as error:
            self.harness_error = "sign_in"
            if str(error).startswith(_SIGN_IN_REFUSED):
                run.refused_groups.add(flow.persona.group)
                run.say(
                    f"sign-in: refused for {flow.persona.group}, so its remaining "
                    "flows are not run"
                )
            run.say(f"flow {flow.key}: harness error (sign_in): {error}")
            return
        except _STEP_FAILURES as error:
            last = self.steps[-1] if self.steps else ""
            self.harness_error = _STEP_ERRORS.get(last, "launch")
            run.say(f"flow {flow.key}: harness error ({self.harness_error}): {error}")
            return
        attempt.opened = True
        attempt.facts = opened.facts
        run.note_facts(opened.facts)
        attempt.withheld = session.review_stack_problem(
            run.plan_stack, run.ui_env, opened.facts.stack_name
        )
        if attempt.withheld:
            run.say(f"video: withheld for {flow.key}, because {attempt.withheld}")
        reached = stories.since_reached(flow.story.since, opened.facts.version)
        if reached is False:
            self.not_run_reason = "since"
            run.say(
                f"flow {flow.key}: not run, the stack is older than {flow.story.since}"
            )
            return
        if run.records(attempt):
            try:
                self._start_recording(opened.target)
            except recording.RecorderError as error:
                self.harness_error = "recorder"
                run.say(f"flow {flow.key}: harness error (recorder): {error}")
                return
        try:
            self._session(run_dir)
        except (agent.AgentError, StoryRunError, OSError) as error:
            self.harness_error = "session"
            run.say(f"flow {flow.key}: harness error (session): {error}")
            return
        self._end_recording()
        try:
            self.browser.close(on_audit=self._note_audit)
        except egress.ProxyError as refused:
            run.say(f"WARNING: the proxy's refusals for {flow.key}: {refused}")
        attempt.refusals = list(self.browser.refusals)

    def _make_run_dir(self) -> Path:
        base = "/tmp" if os.path.isdir("/tmp") else None  # nosec B108 - see FlowSession
        run_dir = Path(tempfile.mkdtemp(prefix="ux-test-stories-", dir=base))
        self.run_dir = run_dir
        os.chmod(run_dir, 0o711)  # nosec B103 - traverse-only, no listing
        return run_dir

    def _start_recording(self, target: Mapping[str, Any]) -> None:
        """The recorder on the signed-in page, opening on the story's own title."""
        run, flow = self.run, self.attempt.flow
        if self.run_dir is None:
            raise recording.RecorderError("the flow has no run directory")
        self.rec_out = self.run_dir / "recordings"
        self.recorder_env = recording.recorder_env(
            artifacts.private_dir(self.run_dir / "recorder-home")
        )
        started = run.runtime.start_recorder(
            harness_root=paths.HARNESS_ROOT,
            out_dir=self.rec_out,
            stack=run.plan_stack,
            persona=flow.key.persona,
            flows=[str(flow.key)],
            target_id=target["id"],
            say=flow.story.title,
            env=self.recorder_env,
        )
        self.session_dir = Path(started["session_dir"])
        run.say(f"recording: started for {flow.key}")

    def _session(self, run_dir: Path) -> None:
        """The flow's ``claude -p`` session, with a Bedrock-only session minted now.

        The story server is its one MCP server, launched under ``env -i`` with the
        flow's start record and step budget, and writing only under ``story/``
        (which it creates) and the recorder's directory. ``claude``'s working
        directory, home and temporary directory are private directories of the
        attempt, and the MCP config is written ``0o600``.
        """
        run, attempt = self.run, self.attempt
        flow = attempt.flow
        aws = run.claude_credentials(attempt)
        agent_root = artifacts.private_dir(run_dir / "agent")
        cwd = artifacts.private_dir(agent_root / "cwd")
        home = artifacts.private_dir(agent_root / "home")
        tmp = artifacts.private_dir(agent_root / "tmp")
        start = run.write_start(agent_root / "start.json", attempt, mode="unattended")
        config = agent.write_mcp_config(
            agent_root / "mcp.json",
            agent.story_server(
                toolchain=run.toolchain,
                story_adapter=paths.HARNESS_ROOT / STORY_SERVER_SCRIPT,
                index=run.index_file,
                out=run_dir / "story",
                start=start,
                budget=attempt.budget,
                python=sys.executable,
                home=home,
                recorder_out=self.rec_out,
                search_path=_search_path(),
            ),
        )
        argv = agent.claude_argv(
            run.toolchain.claude,
            config,
            max_turns=stories.turn_cap(attempt.budget),
            max_budget_usd=attempt.cap_usd,
            profile=tool_profiles.STORY,
            effort=run.effort,
        )
        env = agent.child_env(
            home=home,
            tmpdir=tmp,
            model=run.model,
            region=run.args.region,
            aws=aws,
            source=run.runtime.environ,
        )
        text = run.prompt_for(flow, attempt.budget)
        minutes = run.catalogue.limits.flow_minutes
        run.say(
            f"session: {flow.key} attempt {attempt.number}, {attempt.budget} steps, "
            f"at most {minutes} min and ${attempt.cap_usd:.2f}"
        )
        began = run.runtime.wall()
        try:
            result, completed = run.runtime.run_agent(
                text, argv=argv, cwd=cwd, env=env, timeout=minutes * 60
            )
        except BaseException:
            self.outcome = run.unread_outcome(attempt, began, run.runtime.wall())
            raise
        ended = run.runtime.wall()
        self.outcome = run.session_outcome(attempt, result, completed, began, ended)
        if result is not None:
            self.report = result.text

    def _end_recording(self) -> None:
        """Stop the recorder and keep its session for render; a failed stop loses it."""
        run = self.run
        if self.rec_out is None or self.session_dir is None:
            return
        try:
            run.runtime.stop_recorder(
                harness_root=paths.HARNESS_ROOT,
                out_dir=self.rec_out,
                session_dir=self.session_dir,
                say=_RECORDER_STOP_SAY,
                env=self.recorder_env,
            )
        except recording.RecorderError as error:
            run.say(
                f"WARNING: the recording of {self.attempt.flow.key} is lost: {error}"
            )
            self.session_dir = None

    def _note_audit(self, audit: chrome.SocketAudit) -> None:
        self.audit = audit
        strays = self.browser.strays
        tail = f", {len(strays)} stray process(es) killed" if strays else ""
        self.run.say(f"socket audit ({self.attempt.flow.key}): {audit.state}{tail}")

    def cleanup(self) -> None:
        """Stop everything the attempt started, each step whatever the others did."""
        steps = (
            self._close_session,
            self._stop_recorder,
            self._stop_chrome,
            self._stop_watch,
            self._stop_proxy,
            self._copy_outputs,
            self._remove_run_dir,
        )
        for step in steps:
            try:
                step()
            except (processes.Terminated, KeyboardInterrupt):
                self.run.cancelled = True
            except Exception as error:
                self.run.say(
                    f"cleanup: {step.__name__.strip('_')}: {type(error).__name__}"
                )

    def _close_session(self) -> None:
        self.browser.close_session()

    def _stop_recorder(self) -> None:
        if self.rec_out is not None and (self.rec_out / _RECORDER_CURRENT).exists():
            self.run.runtime.kill_recorder(self.rec_out)

    def _stop_chrome(self) -> None:
        self.browser.stop_chrome()

    def _stop_watch(self) -> None:
        self.browser.stop_watch(on_audit=self._note_audit)

    def _stop_proxy(self) -> None:
        self.browser.stop_proxy()

    def _copy_outputs(self) -> None:
        """The journal, screens, report, refusals and audit into the artifacts."""
        if self.copied:
            return
        self.copied = True
        run, attempt = self.run, self.attempt
        folder = artifacts.artifact_dir(run.artifacts / attempt.folder)
        story_dir = self.run_dir / "story" if self.run_dir is not None else None
        if story_dir is not None:
            self.journal_copied = run.copy_journal(story_dir, folder, attempt)
            run.copy_screens(story_dir, folder, attempt)
        if self.report is not None:
            artifacts.write_artifact(folder / "report.md", run.scrub(self.report))
        proxy = self.browser.proxy
        if proxy is not None and not attempt.refusals:
            with contextlib.suppress(egress.ProxyError):
                attempt.refusals = egress.read_refusals(
                    proxy.refusal_log, owner=proxy.owner
                )
        if proxy is not None:
            lines = [
                json.dumps(
                    run.scrub.tree(chrome.with_origin(record)), ensure_ascii=False
                )
                for record in attempt.refusals
            ]
            artifacts.write_artifact(
                folder / "refusals.jsonl", "".join(line + "\n" for line in lines)
            )
        if self.audit is not None:
            artifacts.write_json(
                folder / "audit.json",
                {
                    "state": self.audit.state,
                    "violations": list(self.audit.violations),
                    "sockets": self.audit.sockets,
                    "strays": list(self.browser.strays),
                },
                run.scrub,
            )

    def _remove_run_dir(self) -> None:
        """Remove the run directory, or keep only its recordings for render."""
        run_dir, self.run_dir = self.run_dir, None
        if run_dir is None:
            return
        attempt = self.attempt
        attempt.context = self.context()
        if self.session_dir is not None and self.run.keeps_recording(attempt):
            for entry in run_dir.iterdir():
                if entry.name != "recordings":
                    shutil.rmtree(entry, ignore_errors=True)
            attempt.recording = self.session_dir
            attempt.keep = run_dir
            self.run.kept.append(run_dir)
            return
        if self.run.args.keep_run_dir:
            self.run.say(f"run directory kept: {run_dir}")
            return
        shutil.rmtree(run_dir, ignore_errors=True)

    def context(self) -> story_verdict.FlowContext:
        """The attempt's record, from what it reached."""
        attempt = self.attempt
        base = attempt.context
        audit_state = self.audit.state if self.audit is not None else "not_run"
        return dataclasses.replace(
            base,
            journal=self.journal_copied,
            not_run_reason=self.not_run_reason,
            harness_error=self.harness_error if self.not_run_reason is None else None,
            audit_state=audit_state,
            strays=len(self.browser.strays),
            session=self.outcome,
            refusals_by_origin=chrome.refusals_by_origin(attempt.refusals),
        )


@dataclass
class Prepared:
    """What every flow of a run shares, made once before the first flow starts."""

    catalogue: stories.Catalogue
    toolchain: paths.Toolchain
    versions: dict[str, str]
    texts: story_prompt.PromptTexts
    series: stories.SeriesInputs
    sts: Any
    identity: account.Identity
    ui_env: dict[str, str]
    personas: dict[str, signin.Persona]
    docs: DocsCorpus
    index_file: Path
    harness_sha: str
    harness_dirty: bool | None
    job_usd: float


def _key_shown(key: object) -> str:
    """A flow key verbatim when it has a flow key's closed shape, else ``?``."""
    text = key if isinstance(key, str) else ""
    try:
        return str(stories.FlowKey.parse(text))
    except ValueError:
        return "?"


def video_note_lines(record: Mapping[str, Any]) -> list[str]:
    """The reply's lines about videos, from ``story-run.json``'s structured fields.

    One line per flow whose video was withheld, with that flow's reason, then
    one saying why only the review stack's flows get a video; then one line per
    other note (a dry run's local file, a failed upload, a video over the upload
    limit, a render that failed). A reason is sanitised, and a key is shown only
    in its closed shape, so a record from elsewhere cannot write markup here.
    """
    lines: list[str] = []
    withheld = record.get("video_withheld_flows")
    for item in withheld if isinstance(withheld, list) else []:
        if not isinstance(item, Mapping):
            continue
        reason = section.sanitize(item.get("reason"), 300)
        if reason:
            lines.append(
                _WITHHELD_LINE.format(key=_key_shown(item.get("key")), reason=reason)
            )
    if lines:
        lines.append(_WITHHELD_WHY.format(stack=session.REVIEW_STACK))
    reply = record.get("reply") if isinstance(record.get("reply"), Mapping) else {}
    notes = reply.get("video_notes") if isinstance(reply, Mapping) else None
    for item in notes if isinstance(notes, list) else []:
        if not isinstance(item, Mapping):
            continue
        key = _key_shown(item.get("key"))
        kind = item.get("kind")
        detail = section.sanitize(item.get("detail"), 120).replace("`", "'")
        if kind == "local" and detail:
            lines.append(_LOCAL_VIDEO_LINE.format(key=key, name=detail))
        elif kind == "upload_failed":
            lines.append(_UPLOAD_FAILED_LINE.format(key=key))
        elif kind == "over_limit" and detail:
            lines.append(_OVER_LIMIT_LINE.format(key=key, size=detail))
        elif kind == "not_rendered":
            lines.append(_NOT_RENDERED_LINE.format(key=key))
    return lines


def with_notes(reply: str, lines: Sequence[str]) -> str:
    """``reply`` with ``lines`` as one paragraph just before its closing footer.

    That is where the reply's layout puts what is said about videos: after the
    video lines and before the advisory footer.
    """
    if not lines:
        return reply
    block = "\n".join(lines)
    at = reply.rfind("\n\n" + _FOOTER_PREFIX)
    if at < 0:
        return reply.rstrip("\n") + "\n\n" + block + "\n"
    return reply[:at] + "\n\n" + block + reply[at:]


def compose_section(
    record: Mapping[str, Any],
    results: Sequence[story_verdict.FlowResult],
    found: Sequence[story_verdict.Finding],
) -> str | None:
    """The reply ``story-run.json``'s ``reply`` and the judged flows make, unredacted.

    None when the record has no reply to make. Every value the reply needs comes
    from the record and the journals, which is what lets ``--judge`` rewrite
    ``section.md`` exactly as the run wrote it.
    """
    reply = record.get("reply")
    if not isinstance(reply, Mapping):
        return None
    stack = record.get("stack") if isinstance(record.get("stack"), Mapping) else {}
    chosen = [
        changelog_select.ChosenLine(
            kind=str(item.get("kind") or ""),
            lead=str(item.get("lead") or ""),
            story=item.get("story") if isinstance(item.get("story"), str) else None,
            covered_by=(
                item.get("covered_by")
                if isinstance(item.get("covered_by"), str)
                else None
            ),
        )
        for item in reply.get("chosen") or []
        if isinstance(item, Mapping)
    ]
    chosen_by = reply.get("chosen_by")
    try:
        text = story_section.compose_reply(
            head_sha=str(reply.get("head_sha") or ""),
            sel=str(reply.get("sel") or ""),
            uploads=[int(upload) for upload in reply.get("uploads") or []],
            results=results,
            found=found,
            chosen=chosen,
            uncovered=[str(lead) for lead in reply.get("uncovered") or []],
            caveats=[str(caveat) for caveat in reply.get("caveats") or []],
            videos=[str(line) for line in reply.get("videos") or []],
            model=str(record.get("model") or ""),
            stack=str(stack.get("name") or ""),
            chosen_by=chosen_by if isinstance(chosen_by, Mapping) else None,
        )
    except (TypeError, ValueError) as error:
        raise story_verdict.RunRecordError(
            f"story-run.json's reply cannot be composed: {error}"
        ) from None
    return with_notes(text, video_note_lines(record))


def section_text(
    record: Mapping[str, Any],
    results: Sequence[story_verdict.FlowResult],
    found: Sequence[story_verdict.Finding],
    scrub: artifacts.Scrub,
) -> str | None:
    """:func:`compose_section` redacted for a note, its marker and video lines exact."""
    text = compose_section(record, results, found)
    if text is None:
        return None
    first = text.split("\n", 1)[0]
    scrub.keep(first, *story_section.STORY_VIDEO_LINE_RE.findall(text))
    return scrub.note(text)


def results_text(
    results: Sequence[story_verdict.FlowResult],
    found: Sequence[story_verdict.Finding],
    scrub: artifacts.Scrub,
    watched: object = None,
) -> str:
    """``results.json`` exactly as ``story_verdict.py judge`` writes it, given the
    run record's ``interactive`` as ``watched``."""
    scrub.keep(*sorted({result.series for result in results}))
    return story_verdict.dump_json(
        scrub.tree(story_verdict.results_document(results, found, watched))
    )


def _first_difference(ours: object, theirs: object, path: str) -> str | None:
    """The first JSON path where ``theirs`` departs from ``ours``, named from ours."""
    if isinstance(ours, dict):
        if not isinstance(theirs, dict):
            return path
        keys = list(theirs)
        for index, key in enumerate(ours):
            child = (
                f"{path}.{key}"
                if re.fullmatch(r"[A-Za-z_]\w*", key)
                else (f"{path}[{json.dumps(key)}]")
            )
            if index >= len(keys) or keys[index] != key:
                return child
            found = _first_difference(ours[key], theirs[key], child)
            if found is not None:
                return found
        return path if len(keys) != len(ours) else None
    if isinstance(ours, list):
        if not isinstance(theirs, list):
            return path
        for index, item in enumerate(ours):
            if index >= len(theirs):
                return f"{path}[{index}]"
            found = _first_difference(item, theirs[index], f"{path}[{index}]")
            if found is not None:
                return found
        return f"{path}[{len(ours)}]" if len(theirs) > len(ours) else None
    if type(ours) is not type(theirs) or ours != theirs:
        return path
    return None


def _difference(name: str, text: str, target: Path) -> str | None:
    """None when ``target`` holds exactly ``text``; else where they first differ.

    For ``results.json`` that is a JSON path, and for ``section.md`` a line.
    """
    try:
        existing = _read_bounded(target, _RECORD_MAX_BYTES, name)
    except StoryRunError:
        return "$" if name.endswith(".json") else "line 1"
    if existing == text.encode("utf-8"):
        return None
    if not name.endswith(".json"):
        theirs = existing.decode("utf-8", errors="replace").split("\n")
        for number, line in enumerate(text.split("\n"), start=1):
            if number > len(theirs) or theirs[number - 1] != line:
                return f"line {number}"
        return f"line {len(text.split(chr(10))) + 1}"
    try:
        theirs = json.loads(existing.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "$"
    return _first_difference(json.loads(text), theirs, "$") or "$"


@dataclass
class Judged:
    """A run directory judged: its flows, findings and the record they came from."""

    record: dict[str, Any]
    results: list[story_verdict.FlowResult]
    found: list[story_verdict.Finding]
    differences: list[tuple[str, str]]


def judge_run_dir(
    run_dir: Path, environ: Mapping[str, str], *, check: bool = False
) -> Judged:
    """Judge ``run_dir`` and write, or with ``check`` compare, its two outputs.

    ``results.json`` is ``story_verdict``'s, written as its own CLI writes it, and
    ``section.md`` is the reply the record's ``reply`` and the results make
    (:func:`section_text`). Both are redacted by the redactor of ``environ`` with
    the computed hashes kept, which is all the run itself writes them with, so a
    run directory re-judged anywhere gives the same bytes. With ``check`` nothing
    is written and each file that would change is listed with where it differs.
    """
    record = _read_json(run_dir / story_verdict.RUN_FILE, story_verdict.RUN_FILE)
    if not isinstance(record, dict):
        raise story_verdict.RunRecordError("story-run.json is not a JSON object")
    results, found = story_verdict.judge_run(run_dir)
    texts = [
        (
            story_verdict.RESULTS_FILE,
            results_text(
                results, found, _judge_scrub(environ), record.get("interactive")
            ),
        )
    ]
    section = section_text(record, results, found, _judge_scrub(environ))
    if section is not None:
        texts.append(("section.md", section))
    differences: list[tuple[str, str]] = []
    for name, text in texts:
        target = run_dir / name
        if check:
            where = _difference(name, text, target)
            if where is not None:
                differences.append((name, where))
        else:
            artifacts.write_artifact(target, text)
    return Judged(record, results, found, differences)


def _judge_scrub(environ: Mapping[str, str]) -> artifacts.Scrub:
    return artifacts.Scrub(artifacts.redactor_for(environ))


def _attempt_label(result: story_verdict.FlowResult) -> str:
    confirmation = story_verdict.confirm([result])
    return story_verdict.verdict_label(result, confirmation)


def flow_lines(results: Sequence[story_verdict.FlowResult]) -> list[str]:
    """One line per flow: its verdict, the steps used against the budget, its hesitations."""
    lines = []
    for key, confirmation, shown in story_verdict.flow_confirmations(results):
        label = story_verdict.verdict_label(shown, confirmation)
        if isinstance(shown.actions_used, int) and isinstance(shown.budget, int):
            steps = f"{shown.actions_used} of {shown.budget} steps"
        else:
            steps = "steps not counted"
        count = len(shown.notes.confusion)
        lines.append(
            f"flow {key}: {label}; {steps}; {count} hesitation"
            + ("" if count == 1 else "s")
        )
    return lines


class StoryRun:
    """One run: the precheck's plan, every attempt, and the records they make.

    The per-flow confinement is :class:`FlowSession`'s. What is shared is made once
    in :meth:`_prepare` (the catalogue, the toolchain, the docs index, the
    account check), and the job's budget and time gates are applied before each
    attempt, so a flow either runs within its caps or is recorded as not run with
    the reason.
    """

    def __init__(self, args: argparse.Namespace, runtime: Runtime) -> None:
        self.args = args
        self.runtime = runtime
        self.scrub = artifacts.Scrub(artifacts.redactor_for(runtime.environ))
        self.artifacts = Path(args.artifact_dir)
        self.plan: Plan | None = None
        self.ready: Prepared | None = None
        self.deadline: float | None = None
        self.flows: list[Flow] = []
        self.attempts: list[Attempt] = []
        self.spent = 0.0
        self.refused_groups: set[str] = set()
        self.facts_seen: list[signin.PageFacts] = []
        self.reruns_not_run: list[dict[str, str]] = []
        self.current: FlowSession | WatchSession | None = None
        self.interactive: dict[str, Any] | None = None
        self.kept: list[Path] = []
        self.work_dir: Path | None = None
        self.scopes: dict[str, str | None] = {"claude": None, "render": None}
        self.cancelled = False
        self.recorded = False
        self.video_notes: list[dict[str, str]] = []
        self.uploads: list[int] = []
        self.video_lines: list[str] = []
        self.warned_ambient = False
        self.cost: str | None = None

    @property
    def prepared(self) -> Prepared:
        if self.ready is None:
            raise StoryRunError("the run was not prepared")
        return self.ready

    @property
    def planned(self) -> Plan:
        if self.plan is None:
            raise StoryRunError("the run has no plan")
        return self.plan

    @property
    def catalogue(self) -> stories.Catalogue:
        return self.prepared.catalogue

    @property
    def toolchain(self) -> paths.Toolchain:
        return self.prepared.toolchain

    @property
    def index_file(self) -> Path:
        return self.prepared.index_file

    @property
    def ui_env(self) -> dict[str, str]:
        return self.prepared.ui_env

    @property
    def plan_stack(self) -> str:
        return self.planned.stack

    @property
    def model(self) -> str:
        return self.planned.model

    @property
    def effort(self) -> str:
        return self.planned.effort

    def say(self, text: str) -> None:
        _say(self.scrub, text)

    def execute(self) -> int:
        """Run, clean up, and leave the run's records whatever happened.

        Except into an artifact directory that is a symlink: that is refused before
        anything starts, and nothing at all is written through it.
        """
        try:
            artifacts.artifact_dir(self.artifacts)
        except review.Failure as refused:
            self.say(f"FAILED: {refused}")
            return 1
        return self._guarded()

    def _guarded(self) -> int:
        """:meth:`_run` inside the cleanup, with every outcome mapped to a status."""
        try:
            try:
                code = self._run()
            finally:
                self._cleanup()
        except (processes.Terminated, KeyboardInterrupt):
            self._record_after_failure()
            return self._finish("failed", "cancelled")
        except review.Skip as skip:
            self._record_after_failure()
            return self._finish("skipped", str(skip))
        except _STEP_FAILURES as failure:
            self._record_after_failure()
            return self._finish("failed", str(failure))
        except Exception as error:
            self._record_after_failure()
            return self._finish("failed", _unexpected(self.scrub, error))
        if self.cancelled:
            return self._finish("failed", "cancelled")
        return code

    def _finish(self, status: str, detail: str) -> int:
        """Print the run's final status line, with what it cost once it judged flows."""
        cost = f"; {self.cost}" if self.cost else ""
        self.say(f"{status.upper().replace('_', ' ')}: {detail}{cost}")
        if status == "failed":
            return 1
        return 1 if status == "skipped" and self.args.fail_on_skip else 0

    def _run(self) -> int:
        decision = precheck(self.args, self.runtime, self.scrub)
        self.plan = decision.plan
        for warning in decision.extra.get("warnings", []):
            self.say(f"WARNING: {warning}")
        if not decision.proceed:
            if decision.reply is not None and self.args.post:
                post_line(self.args, self.runtime, self.scrub, decision.reply)
            return self._finish(decision.status, decision.detail)
        plan = self.planned
        self.say(f"PROCEED: {decision.detail} at {plan.head_sha[:8]}")
        self._prepare()
        if plan.mode == "watch":
            self._watch()
        else:
            self._run_flows()
        return self._conclude()

    def _prepare(self) -> None:
        """The toolchain, the account, the series, the docs index, the flows."""
        args, rt, plan = self.args, self.runtime, self.planned
        environ = rt.environ
        catalogue = load_catalogue()
        tc = paths.toolchain(args.toolchain_prefix, environ)
        absent = tc.missing()
        if absent:
            raise StoryRunError(
                f"the UX toolchain under {tc.prefix} has no {', '.join(absent)}; "
                "run scripts/ux_runner/install.sh"
            )
        versions = tc.versions()
        pinned = pinned_versions()
        drifted = [
            key
            for key in _TOOLCHAIN_KEYS
            if versions.get(key) not in (None, pinned[key])
        ]
        if drifted:
            self.say(
                "WARNING: the installed toolchain is not the harness's pin for "
                f"{', '.join(drifted)}, so its series differ from the precheck's"
            )
        if args.dev_unconfined:
            self.say(
                "--dev-unconfined: no managed policy and no uid switch, so nothing "
                "is posted"
            )
        elif not chrome.policy_installed(chrome.load_policy()):
            raise StoryRunError(
                "the managed Chrome policy is not installed as "
                "scripts/ux_runner/chrome_policy.json says; run "
                "scripts/ux_runner/install.sh"
            )
        texts = read_texts()
        series = series_inputs(
            model=plan.model,
            effort=plan.effort,
            prompt_revision=story_prompt.STORY_PROMPT_REVISION,
            versions=versions,
            texts=texts,
        )
        sts = rt.sts(args.region)
        identity = account.caller_identity(sts)
        self.scrub.redactor.add(identity.arn)
        try:
            account.check_account(identity, environ.get("UX_TEST_AWS_ACCOUNT_ID", ""))
        except account.AccountMismatch as mismatch:
            raise review.Skip(str(mismatch)) from None
        self.say(
            "account: the credentials reach the expected account, ending in "
            f"{identity.account[-4:]}"
        )
        ui_env = egress.ui_env_from_b64(environ.get("UX_TEST_UI_ENV", ""))
        personas: dict[str, signin.Persona] = {}
        if plan.mode != "watch":
            personas = signin.personas_from_env(environ.get("UX_TEST_PERSONAS", ""))
        if plan.web_url:
            self._keep_host_secret(plan.web_url)
        harness_sha, harness_dirty = rt.harness(environ)
        base = "/tmp" if os.path.isdir("/tmp") else None  # nosec B108 - private dir
        self.work_dir = Path(tempfile.mkdtemp(prefix="ux-test-stories-docs-", dir=base))
        os.chmod(self.work_dir, 0o700)
        docs = rt.index_docs(
            repo_dir=Path(args.repo_dir), ref=plan.docs_ref, work_dir=self.work_dir
        )
        index_file = docs_index.save(docs.index, self.work_dir / "index.json")
        self.say(
            f"docs: {len(docs.index.pages)} published page(s) from "
            + (f"{docs.ref[:8]}" if docs.ref else docs.journal_ref)
        )
        self.flows = [
            Flow(
                key=key,
                story=catalogue.story(key.story),
                persona=catalogue.persona(key.persona),
                expectation=catalogue.story(key.story).expectation_for(key.persona),
                story_hash=stories.story_hash(catalogue, catalogue.story(key.story)),
                series=series_hash,
            )
            for key, series_hash in plan_series(catalogue, plan.keys, series)
        ]
        self.ready = Prepared(
            catalogue=catalogue,
            toolchain=tc,
            versions=versions,
            texts=texts,
            series=series,
            sts=sts,
            identity=identity,
            ui_env=ui_env,
            personas=personas,
            docs=docs,
            index_file=index_file,
            harness_sha=harness_sha,
            harness_dirty=harness_dirty,
            job_usd=self._job_usd(catalogue),
        )
        self.deadline = self._deadline(catalogue)
        self._keep_hashes()

    def _keep_host_secret(self, url: str) -> None:
        """Register the deployed web URL and its host with the redactor."""
        host = urlsplit(url).hostname or ""
        self.scrub.redactor.add(url, host)

    def _keep_hashes(self) -> None:
        """Every computed hash and SHA the artifacts hold, passed through unredacted."""
        ready, plan = self.prepared, self.planned
        kept = [
            plan.head_sha,
            ready.harness_sha,
            ready.docs.index.corpus_hash,
            ready.docs.tree,
            catalogue_sha256(),
            ready.texts.brief_sha256,
            ready.texts.ci_contract_sha256,
            *(flow.series for flow in self.flows),
            *(flow.story_hash for flow in self.flows),
        ]
        if ready.docs.ref:
            kept.append(ready.docs.ref)
        if plan.src_ui_tree:
            kept.append(plan.src_ui_tree)
        if plan.inputs is not None:
            kept += [plan.inputs.mr.head_sha, plan.inputs.mr.base_sha]
        self.scrub.keep(*(value for value in kept if value))

    def _job_usd(self, catalogue: stories.Catalogue) -> float:
        """The job's dollar cap: ``--run-usd``; a CI job's ``job_usd``; else per flow.

        A local container's cap is its flow count times ``flow_usd``, so a local
        run is not cut short by CI's per-job cap.
        """
        if self.args.run_usd is not None:
            return float(self.args.run_usd)
        if self.planned.mode in ("mr", "nightly"):
            return float(catalogue.runs.job_usd)
        return float(len(self.planned.keys)) * float(catalogue.limits.flow_usd)

    def _deadline(self, catalogue: stories.Catalogue) -> float | None:
        """When the run must be done, on :attr:`Runtime.clock`; None for no deadline.

        A CI job counts from ``CI_JOB_STARTED_AT`` with the catalogue's
        ``deadline_minutes`` unless ``--deadline-minutes`` says otherwise; a local
        run has a deadline only when given one. The clock and the job's elapsed
        time are read together, so the time this run took to get here is counted
        once.
        """
        minutes = self.args.deadline_minutes
        if minutes is None and self.planned.mode in ("mr", "nightly"):
            minutes = catalogue.runs.deadline_minutes
        if minutes is None:
            return None
        return self.runtime.clock() + minutes * 60 - _job_elapsed(self.runtime.environ)

    def _time_left(self) -> float | None:
        """Seconds until :attr:`deadline`, or None when the run has no deadline."""
        if self.deadline is None:
            return None
        return self.deadline - self.runtime.clock()

    def _out_of_time(self, needed: float) -> bool:
        """Whether fewer than ``needed`` seconds are left; never, with no deadline."""
        left = self._time_left()
        return left is not None and left < needed

    def _timeout(self, *, least: float, most: float) -> float:
        """A step's timeout: the time left, kept between ``least`` and ``most``.

        With no deadline it is ``most``, so a timeout is always a finite number of
        seconds, which ``select`` and every wait below it need.
        """
        left = self._time_left()
        return most if left is None else min(max(least, left), most)

    def browser_plan(self, flow: Flow, run_dir: Path) -> session.BrowserPlan:
        """The confined browser one attempt of ``flow`` is opened with."""
        plan = self.planned
        persona = self.prepared.personas[flow.persona.group]
        return session.BrowserPlan(
            toolchain=self.toolchain,
            run_dir=run_dir,
            ui_env=self.ui_env,
            stack=plan.stack,
            persona=persona,
            persona_label=f"{flow.key.persona} ({flow.persona.group})",
            build_dir=plan.build_dir if plan.ui_kind == "build" else None,
            dev_unconfined=self.args.dev_unconfined,
            web_url=plan.web_url if plan.ui_kind == "deployed" else None,
            dismiss_welcome=not flow.story.first_visit,
        )

    def note_facts(self, facts: signin.PageFacts) -> None:
        self.facts_seen.append(facts)

    def records(self, attempt: Attempt) -> bool:
        """Whether an attempt records at all: never one whose video is withheld."""
        return not attempt.withheld and self.args.video != "never"

    def keeps_recording(self, attempt: Attempt) -> bool:
        """Whether an attempt's recording may still be rendered once all are judged.

        Under ``--video all`` every allowed recording is; otherwise one whose
        verdict is bad, or, outside the nightly, one that found a help link with no
        page behind it. A nightly flow is rendered only once its bad verdict is
        confirmed, which only the end of the run can say.
        """
        if attempt.withheld or self.args.video == "never":
            return False
        result = judge_attempt(self.artifacts, attempt.context)
        attempt.result = result
        if self.args.video == "all":
            return True
        if result.verdict in story_verdict.BAD_VERDICTS:
            return True
        return self.planned.mode != "nightly" and bool(result.help_links_unresolved)

    def claude_credentials(self, attempt: Attempt) -> dict[str, str]:
        """The flow's Bedrock-only session, minted now, or the job's own.

        A refused self-assume is the usual case on a developer's SSO session; then
        ``claude`` runs on the ambient credentials, a ``WARNING:`` line says so
        once, and ``credential_scope.claude`` records ``ambient``, at its weakest
        across the run's flows.
        """
        ready = self.prepared
        name = f"ux-story-{self.planned.mode}-{len(self.attempts)}"
        minted = session.mint_bedrock(ready.sts, ready.identity, name)
        if minted is None:
            self.scopes["claude"] = "ambient"
            if not self.warned_ambient:
                self.warned_ambient = True
                self.say(
                    "WARNING: no Bedrock-only session could be minted, so claude runs "
                    "on the job's own AWS credentials (credential_scope.claude: "
                    "ambient)"
                )
            return agent.aws_env_from(self.runtime.environ)
        self.scrub.redactor.add(*minted.values())
        if self.scopes["claude"] is None:
            self.scopes["claude"] = "scoped"
        return minted

    def write_start(self, path: Path, attempt: Attempt, *, mode: str) -> Path:
        """The story server's start record, written ``0o600`` and never over a file."""
        ready, flow = self.prepared, attempt.flow
        record = {
            "story": flow.story.id,
            "story_hash": flow.story_hash,
            "series": flow.series,
            "persona": flow.key.persona,
            "group": flow.persona.group,
            "expectation": flow.expectation,
            "kind": flow.story.kind,
            "attempt": attempt.number,
            "docs_ref": ready.docs.journal_ref,
            "corpus_hash": ready.docs.index.corpus_hash,
            "harness": journal_harness(ready.harness_sha, ready.harness_dirty),
            "model": self.model,
            "effort": self.effort,
            "mode": mode,
        }
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        return path

    def prompt_for(self, flow: Flow, budget: int) -> str:
        """The flow's prompt: catalogue text, the skills and the bounds, nothing else."""
        return self.runtime.compose_prompt(
            story=flow.story,
            persona=flow.persona,
            budget=budget,
            texts=self.prepared.texts,
        )

    def session_outcome(
        self,
        attempt: Attempt,
        result: agent.AgentResult | None,
        completed: processes.Completed,
        began: float,
        ended: float,
    ) -> story_verdict.SessionOutcome:
        """The session's record, and what the budget counts for it."""
        usage = result.usage if result is not None else None
        cost = result.cost_usd if result is not None else None
        pinned = pinned_cost(self.model, usage)
        if result is not None:
            stop: str | None = result.stop
        else:
            stop = "timeout" if completed.timed_out else None
        return story_verdict.SessionOutcome(
            started_epoch=round(began, 3),
            ended_epoch=round(ended, 3),
            timed_out=completed.timed_out,
            returncode=completed.returncode,
            stop=stop,
            is_error=result.is_error if result is not None else False,
            turns=result.num_turns if result is not None else None,
            duration_s=result.duration_s if result is not None else None,
            cost_usd=cost,
            cost_usd_pinned=pinned,
            counted_usd=counted_cost(cost, pinned, attempt.cap_usd),
            usage=dict(usage) if usage else None,
        )

    def unread_outcome(
        self, attempt: Attempt, began: float, ended: float
    ) -> story_verdict.SessionOutcome:
        """A session that ran but whose answer cannot be read: counted at its cap.

        The CLI was paid for whatever it did, so a flow whose cost cannot be read is
        counted as having spent its whole cap rather than nothing.
        """
        return story_verdict.SessionOutcome(
            started_epoch=round(began, 3),
            ended_epoch=round(ended, 3),
            timed_out=False,
            returncode=-1,
            stop=None,
            is_error=True,
            turns=None,
            duration_s=None,
            cost_usd=None,
            cost_usd_pinned=None,
            counted_usd=counted_cost(None, None, attempt.cap_usd),
            usage=None,
        )

    def copy_journal(
        self, story_dir: Path, folder: Path, attempt: Attempt
    ) -> str | None:
        """The server's journal, redacted, into ``folder``; its path in the run, or None.

        Read back as a regular file of this process's uid directly in the server's
        directory, within the journal's bound, because the server and the
        chrome-devtools-mcp it starts both run as this uid on the model's input.
        """
        try:
            raw = readback.read(
                story_dir,
                "journal.jsonl",
                writer=_SERVER,
                owner=os.geteuid(),
                limit=story_verdict.MAX_JOURNAL_BYTES,
            )
        except StoryRunError as refused:
            self.say(
                f"WARNING: the journal of {attempt.flow.key} is refused: {refused}"
            )
            return None
        if raw is None:
            return None
        with artifacts.open_artifact(folder / "journal.jsonl") as handle:
            handle.write(redacted_journal(raw, self.scrub))
        return story_verdict.journal_path(attempt.context.key, attempt.number)

    def copy_screens(self, story_dir: Path, folder: Path, attempt: Attempt) -> None:
        """The relay's screenshots into ``folder/screens``, each read back as the journal is."""
        screens = story_dir / _SCREENS_DIR
        try:
            names = sorted(
                entry.name
                for entry in os.scandir(screens)
                if _SCREEN_NAME_RE.fullmatch(entry.name)
            )
        except FileNotFoundError:
            return
        except OSError as error:
            self.say(
                f"WARNING: the screens of {attempt.flow.key} cannot be listed: {error}"
            )
            return
        if not names:
            return
        target = artifacts.artifact_dir(folder / _SCREENS_DIR)
        limit = attempt.budget + story_relay.SCREENS_OVER_BUDGET
        for name in names[:limit]:
            try:
                readback.copy(
                    screens,
                    name,
                    lambda chosen=name: artifacts.open_artifact(target / chosen),
                    writer=_SERVER,
                    owner=os.geteuid(),
                    limit=_SCREEN_BYTES,
                )
            except StoryRunError as refused:
                self.say(
                    f"WARNING: a screen of {attempt.flow.key} is refused: {refused}"
                )

    def _stack_version(self) -> str | None:
        return next(
            (facts.version for facts in self.facts_seen if facts.version is not None),
            None,
        )

    def _not_run_reason(self, attempt: Attempt) -> str | None:
        """Why an attempt does not start, in the order the run learns each reason."""
        flow = attempt.flow
        group = flow.persona.group
        if group in self.refused_groups:
            return "sign_in_refused"
        if group not in self.prepared.personas:
            return "persona"
        version = self._stack_version()
        if (
            version is not None
            and stories.since_reached(flow.story.since, version) is False
        ):
            return "since"
        if self.prepared.job_usd - self.spent < attempt.cap_usd - 1e-9:
            return "budget"
        limits, runs = self.catalogue.limits, self.catalogue.runs
        if self._out_of_time((limits.flow_minutes + runs.reserve_minutes) * 60):
            return "time"
        return None

    def _new_attempt(self, flow: Flow, number: int, scale: int) -> Attempt:
        limits, runs = self.catalogue.limits, self.catalogue.runs
        budget = stories.budget(limits, flow.story, scale=scale)
        cap = runs.extended_flow_usd if scale > 1 else limits.flow_usd
        context = story_verdict.FlowContext(
            key=str(flow.key),
            story=flow.story.id,
            persona=flow.key.persona,
            group=flow.persona.group,
            title=flow.story.title,
            expectation=flow.expectation,
            kind=flow.story.kind,
            documented_steps=flow.story.documented_steps,
            needs=flow.story.needs,
            declared_docs=flow.story.match.docs,
            series=flow.series,
            budget=budget,
            attempt=number,
            budget_scale=scale,
        )
        return Attempt(flow, number, scale, budget, float(cap), context)

    def _attempt(self, attempt: Attempt) -> None:
        """Run one attempt in its own :class:`FlowSession`, cleaned up whatever happens.

        Its spend is counted whatever happens too: a session the run is cancelled
        in, whose answer is never read, counts at its whole cap.
        """
        self.attempts.append(attempt)
        flow_session = FlowSession(self, attempt)
        self.current = flow_session
        try:
            flow_session.run_attempt()
        finally:
            self.current = None
            flow_session.cleanup()
            attempt.context = flow_session.context()
            outcome = attempt.context.session
            if outcome is not None:
                self.spent = round(self.spent + outcome.counted_usd, 6)
        if attempt.result is None:
            attempt.result = judge_attempt(self.artifacts, attempt.context)
        self.say(
            f"flow {attempt.flow.key} attempt {attempt.number}: "
            f"{_attempt_label(attempt.result)}"
        )

    def _skip_attempt(self, attempt: Attempt, reason: str) -> None:
        attempt.context = dataclasses.replace(attempt.context, not_run_reason=reason)
        attempt.result = judge_attempt(self.artifacts, attempt.context)
        self.attempts.append(attempt)
        self.say(f"flow {attempt.flow.key}: not run ({reason.replace('_', ' ')})")

    def _rerun_scale(self, first: Attempt) -> int | None:
        """The budget scale a flow's one re-run gets, or None for no re-run.

        At night a flow that ran out of steps or gave up is run once more at
        twice its budget, when that changes what stopped it: the steps, the turn
        cap or the dollar cap. A wall clock or a refusal limit stays as it was,
        so a flow they stopped is not run again for them.
        """
        if self.args.reruns < 1 or first.result is None:
            return None
        verdict = first.result.verdict
        if verdict in story_verdict.RERUN_VERDICTS:
            return 1
        if (
            self.planned.mode != "nightly"
            or verdict not in story_verdict.BUDGET_RERUN_VERDICTS
        ):
            return None
        cause = first.result.sub_kind
        if verdict == "gave_up" and cause not in _EXTENDED_CAUSES:
            return None
        if cause == "max_turns":
            wider = stories.budget(self.catalogue.limits, first.flow.story, scale=2)
            if stories.turn_cap(wider) <= stories.turn_cap(first.budget):
                return None
        return 2

    def _run_flows(self) -> None:
        """Every flow's first attempt, then the re-runs, each gated before it starts.

        First attempts go first, so a re-run never costs another flow its first
        attempt. A re-run whose gate does not open is no attempt, so its flow's
        bad verdict stays unconfirmed (1 of 1) rather than reading as mixed; the
        record's ``reruns_not_run`` names it and why, for the digest to count.
        """
        for flow in self.flows:
            attempt = self._new_attempt(flow, 1, 1)
            reason = self._not_run_reason(attempt)
            if reason is not None:
                self._skip_attempt(attempt, reason)
            else:
                self._attempt(attempt)
        firsts = [attempt for attempt in self.attempts if attempt.number == 1]
        for first in firsts:
            scale = self._rerun_scale(first)
            if scale is None:
                continue
            again = self._new_attempt(first.flow, 2, scale)
            reason = self._not_run_reason(again)
            if reason is not None:
                self.reruns_not_run.append(
                    {"key": str(first.flow.key), "reason": reason}
                )
                self.say(
                    f"rerun: {first.flow.key} is not run again "
                    f"({reason.replace('_', ' ')})"
                )
                continue
            self._attempt(again)

    def _quick_results(self) -> list[story_verdict.FlowResult]:
        return [
            attempt.result for attempt in self.attempts if attempt.result is not None
        ]

    def _video_candidates(self) -> list[Attempt]:
        """The attempts whose recordings are rendered, at most :data:`MAX_VIDEOS`.

        One file per flow, of the attempt its confirmation shows: under ``--video
        all`` every flow with a recording; at night only a confirmed bad verdict;
        otherwise a flow with a finding.
        """
        if self.args.video == "never":
            return []
        results = self._quick_results()
        found = story_verdict.findings(results)
        attached = story_verdict.attach_findings(results, found)
        by_key = {
            (attempt.context.key, attempt.number): attempt for attempt in self.attempts
        }
        chosen: list[Attempt] = []
        for _key, confirmation, shown in story_verdict.flow_confirmations(attached):
            attempt = by_key.get((shown.key, shown.attempt))
            if attempt is None or attempt.recording is None:
                continue
            if self.args.video == "all":
                wanted = True
            elif self.planned.mode == "nightly":
                wanted = (
                    confirmation.state == "confirmed"
                    and confirmation.verdict in story_verdict.BAD_VERDICTS
                )
            else:
                wanted = bool(shown.findings)
            if wanted:
                chosen.append(attempt)
        cap = (
            len(chosen)
            if self.args.video == "all" and self.planned.mode == "local"
            else MAX_VIDEOS
        )
        return chosen[:cap]

    def _video_name(self, attempt: Attempt, number: int) -> str:
        plan = self.planned
        if plan.mode == "mr" and self.args.mr is not None:
            return f"ux-story-mr{self.args.mr}-{plan.head_sha[:8]}-{number}.mp4"
        flow = attempt.flow
        return f"ux-story-{flow.key.story}--{flow.key.persona}-{attempt.number}.mp4"

    def _render_videos(self) -> None:
        """Render the chosen recordings, now that every Chrome of the run is gone.

        Each render runs as ``uxrender`` with a Polly-only session minted for it
        (or no credential, and no narration, when none can be), after the
        recording's text has been redacted, and its mp4 and chapters are read back
        as render's own files. A render that fails costs that flow its video and
        nothing else, and the reply says so.
        """
        for number, attempt in enumerate(self._video_candidates(), start=1):
            key = attempt.context.key
            if self._out_of_time(_RENDER_MIN_SECONDS):
                self.say(f"video: not rendered for {key}, no time is left")
                break
            try:
                self._render_one(attempt, number)
            except (recording.RecorderError, review.Failure, OSError) as error:
                self.say(f"WARNING: the video of {key} was not rendered: {error}")
                self.video_notes.append(
                    {"key": key, "kind": "not_rendered", "detail": ""}
                )
        self._drop_kept()

    def _render_one(self, attempt: Attempt, number: int) -> None:
        rt, args = self.runtime, self.args
        session_dir, keep = attempt.recording, attempt.keep
        if session_dir is None or keep is None:
            return
        result = attempt.result
        recording.write_review_md(
            session_dir, self.scrub(self._review_md(attempt, result))
        )
        self._scrub_session(session_dir)
        polly = None if args.no_narration else self._polly_session(attempt)
        home = artifacts.private_dir(keep / "render-home")
        mp4, narration = rt.render_video(
            session_dir,
            harness_root=paths.HARNESS_ROOT,
            voice=args.voice,
            region=args.region,
            narration=polly is not None,
            aws=polly or {},
            home=home,
            log_path=keep / "render.log",
            timeout=self._timeout(least=_RENDER_MIN_SECONDS, most=_RENDER_MAX_SECONDS),
            search_path=_search_path("ffmpeg", recording.recorder_env(home)["PATH"]),
            user=paths.RENDER_USER,
        )
        owner = recording.render_uid(paths.RENDER_USER)
        name = self._video_name(attempt, number)
        size = (
            None
            if mp4 is None
            else recording.copy_output(
                session_dir,
                recording.VIDEO_FILE,
                lambda: artifacts.open_artifact(self.artifacts / name),
                owner=owner,
                limit=recording.MAX_VIDEO_BYTES,
            )
        )
        if size is None:
            raise recording.RecorderError("render finished without writing a video")
        chapters = rt.chapters(session_dir, owner=owner)
        attempt.context = dataclasses.replace(
            attempt.context,
            video=name,
            chapters=tuple(
                (self.scrub.field(at), self.scrub.field(label))
                for at, label in chapters
            ),
        )
        self.video_notes.append(
            {"key": attempt.context.key, "kind": "local", "detail": name}
        )
        self.say(
            f"render: {attempt.context.key}, {size / _MIB:.1f} MB, narration {narration}"
        )

    def _review_md(
        self, attempt: Attempt, result: story_verdict.FlowResult | None
    ) -> str:
        """The recording's ``review.md``: the story, the verdict, the flow's findings.

        Render reads the indented entries under ``Findings`` for the end card, so
        each is one line, and none spells the recorder's own chapter markers.
        """
        flow = attempt.flow
        label = _attempt_label(result) if result is not None else "NOT JUDGED"
        found = story_verdict.findings([result]) if result is not None else []
        lines = [
            f"First-time-user story — {_card(flow.story.title, 120)}, as "
            f"{_card(flow.key.persona, 60)}",
            "",
            "Looked at",
            f"  {_card(label, 160)}",
            "",
            "Findings",
        ]
        lines += [f"  {_card(item.title, 200)}" for item in found] or ["  none"]
        lines += [
            "",
            "Chapters",
            "<!-- ux-recorder:chapters -->",
            "<!-- /ux-recorder:chapters -->",
        ]
        return "\n".join(lines) + "\n"

    def _polly_session(self, attempt: Attempt) -> dict[str, str] | None:
        """Render's Polly-only session, minted now, or None.

        Render gets more only with ``--narrate-with-own-credentials``, which main
        refuses in CI: when no Polly-only session can be minted, it then gets the
        run's own credentials, recorded as ``ambient``, rather than none.
        """
        ready = self.prepared
        polly = session.mint_polly(
            ready.sts, ready.identity, f"ux-story-render-{len(self.attempts)}"
        )
        if polly is None and self.args.narrate_with_own_credentials:
            own = agent.aws_env_from(self.runtime.environ)
            if own.get("AWS_ACCESS_KEY_ID") and own.get("AWS_SECRET_ACCESS_KEY"):
                self.scrub.redactor.add(*own.values())
                self.scopes["render"] = "ambient"
                self.say(
                    f"WARNING: no Polly-only session could be minted for "
                    f"{attempt.context.key}, so its video is narrated with the run's "
                    "own AWS credentials (credential_scope.render: ambient)"
                )
                return own
        if polly is None:
            self.scopes["render"] = "unavailable"
            self.say(
                f"render: no Polly-only session could be minted for "
                f"{attempt.context.key}, so its video has no narration"
            )
            return None
        self.scrub.redactor.add(*polly.values())
        if self.scopes["render"] is None:
            self.scopes["render"] = "scoped"
        return polly

    def _scrub_session(self, session_dir: Path) -> None:
        """Redact the recording's text before render speaks, captions or titles it."""
        path = session_dir / "timeline.json"
        try:
            timeline = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            raise recording.RecorderError(
                "the recording's timeline cannot be read, so it is not rendered"
            ) from None
        events = timeline.get("events") if isinstance(timeline, dict) else None
        if not isinstance(events, list):
            raise recording.RecorderError("the recording's timeline holds no events")
        for event in events:
            if not isinstance(event, dict):
                continue
            for key in _EVENT_TEXT_FIELDS:
                value = event.get(key)
                if isinstance(value, str):
                    event[key] = self.scrub(value)
        path.write_text(json.dumps(timeline, indent=2) + "\n", encoding="utf-8")
        (session_dir / _NARRATION_OVERRIDE).unlink(missing_ok=True)

    def _drop_kept(self) -> None:
        for kept in self.kept:
            shutil.rmtree(kept, ignore_errors=True)
        self.kept = []
        for attempt in self.attempts:
            attempt.recording = None
            attempt.keep = None

    def _caveats(self) -> list[str]:
        """What a reader weighs every verdict against, in reply order."""
        plan = self.planned
        facts = self._first_read_facts()
        region = self.ui_env.get("VITE_AWS_REGION") or self.args.region
        stack = section.StackFacts(
            name=plan.stack,
            region=region,
            version=facts.version if facts is not None else None,
            build=facts.build if facts is not None else None,
        )
        if plan.ui_kind == "build":
            ui_source = f"build:{plan.src_ui_tree}" if plan.src_ui_tree else "build"
        else:
            ui_source = "deployed"
        refusals = [record for attempt in self.attempts for record in attempt.refusals]
        backend = bool(
            plan.change and (plan.change.backend or plan.change.api_contract)
        )
        docs = self.prepared.docs
        caveats = story_section.standard_caveats(
            ui_source=ui_source,
            docs_sha=docs.ref if plan.mode == "mr" else None,
            backend_changed=backend,
            stack=stack,
            refusals=refusals,
            mr=plan.mode == "mr",
        )
        if plan.mode != "mr" and docs.ref:
            caveats = [
                f"docs at `{docs.ref[:8]}`"
                if caveat == "docs from the working tree"
                else caveat
                for caveat in caveats
            ]
        if plan.selection is not None:
            for persona in plan.selection.dropped_personas:
                group = self.catalogue.persona(persona).group
                caveats.append(_DROPPED_CAVEAT.format(persona=persona, group=group))
        if self.facts_seen and self._stack_version() is None:
            caveats.append(_SINCE_CAVEAT)
        return caveats

    def _reply(self) -> dict[str, Any]:
        """The ``reply`` object of ``story-run.json``: everything the reply needs
        beside the judged flows, redacted as fields before it is composed."""
        plan, ready = self.planned, self.prepared
        pairs = [(flow.key, flow.series) for flow in self.flows]
        ui_source, docs_tree = sel_sources(
            ui_kind=plan.ui_kind,
            src_ui_tree=plan.src_ui_tree,
            docs_tree=ready.docs.tree,
        )
        sel = changelog_select.selection_hash(
            pairs, ui_source=ui_source, docs_tree=docs_tree
        )
        self.scrub.keep(sel, ui_source, docs_tree)
        chosen: list[dict[str, Any]] = []
        nudges: list[str] = []
        if plan.selection is not None:
            chosen = [
                {
                    "kind": self.scrub.field(line.kind),
                    "lead": self.scrub.field(line.lead),
                    "story": line.story,
                    "covered_by": line.covered_by,
                }
                for line in plan.selection.chosen
            ]
            nudges = [self.scrub.field(lead) for lead in plan.selection.uncovered]
        return {
            "head_sha": plan.head_sha,
            "sel": sel,
            "ui_source": ui_source,
            "docs_tree": docs_tree,
            "uploads": list(self.uploads),
            "videos": list(self.video_lines),
            "chosen_by": self._chosen_by(),
            "chosen": chosen,
            "uncovered": nudges,
            "caveats": [self.scrub.field(caveat) for caveat in self._caveats()],
            "video_notes": list(self.video_notes),
        }

    def _chosen_by(self) -> dict[str, Any]:
        """How the run's flows were chosen, for the reply's first lines."""
        plan, args = self.planned, self.args
        if plan.mode == "mr":
            return {"kind": "mr"}
        if plan.replayed is not None:
            return {"kind": "replay"}
        if args.all:
            return {"kind": "all", "tier": args.tier, "shard": args.shard}
        return {"kind": "story", "flows": [str(key) for key in plan.keys]}

    def _review_stack(self) -> bool | None:
        opened = [attempt for attempt in self.attempts if attempt.opened]
        if not opened:
            return None
        return all(not attempt.withheld for attempt in opened)

    def _withheld_flows(self) -> list[dict[str, Any]]:
        withheld = [
            {
                "key": attempt.context.key,
                "attempt": attempt.number,
                "reason": attempt.withheld,
            }
            for attempt in self.attempts
            if attempt.withheld
        ]
        return sorted(withheld, key=lambda item: (item["key"], item["attempt"]))

    def run_record(self) -> dict[str, Any]:
        """``story-run.json`` v1: the run context ``--judge`` needs and nothing secret."""
        plan, ready = self.planned, self.prepared
        builds = {f.build for f in self.facts_seen if f.build is not None}
        facts = self._first_read_facts()
        withheld = self._withheld_flows()
        if plan.ui_kind == "build":
            ui_source: dict[str, Any] = {"kind": "build", "head_sha": plan.head_sha}
            if plan.src_ui_tree:
                ui_source["src_ui_tree"] = plan.src_ui_tree
        else:
            ui_source = {"kind": "deployed"}
        docs = ready.docs
        record: dict[str, Any] = {
            "schema": story_verdict.RUN_SCHEMA,
            "version": story_verdict.RUN_VERSION,
            "mode": plan.mode,
            "shard": self._shard_record(),
            "harness_sha": ready.harness_sha,
            "harness_dirty": ready.harness_dirty,
            "catalogue_sha256": catalogue_sha256(),
            "prompt_revision": ready.series.prompt_revision,
            "judgement_revision": story_verdict.JUDGEMENT_REVISION,
            "relay_revision": story_relay.RELAY_REVISION,
            "model": plan.model,
            "effort": plan.effort,
            "region": self.args.region,
            "confinement": "dev-unconfined" if self.args.dev_unconfined else "full",
            "versions": dict(ready.versions),
            "stack": {
                "name": plan.stack,
                "version": facts.version if facts is not None else None,
                "build": facts.build if facts is not None else None,
                "review_stack": self._review_stack(),
            },
            "stack_state": plan.stack_state,
            "stack_changed_mid_run": len(builds) > 1,
            "ui_source": ui_source,
            "docs": {
                "ref": docs.ref,
                "corpus_hash": docs.index.corpus_hash,
                "source": docs.index.source.kind,
                "dirty": docs.index.source.dirty,
            },
            "limits": dataclasses.asdict(self.catalogue.limits),
            "flows": [
                attempt.context.to_json()
                for attempt in sorted(
                    self.attempts, key=lambda item: (item.context.key, item.number)
                )
            ],
            "credential_scope": dict(self.scopes),
            "video_withheld": withheld[0]["reason"] if withheld else None,
            "video_withheld_flows": withheld,
            "spend_usd": round(self.spent, 6),
            "reruns_not_run": list(self.reruns_not_run),
            "reply": self._reply(),
        }
        if plan.replayed is not None:
            record["replayed"] = plan.replayed
        if self.interactive is not None:
            record["interactive"] = dict(self.interactive)
        return record

    def _first_read_facts(self) -> signin.PageFacts | None:
        """The first page facts that read a version or a build, else the first."""
        return next(
            (
                f
                for f in self.facts_seen
                if f.version is not None or f.build is not None
            ),
            next(iter(self.facts_seen), None),
        )

    def _shard_record(self) -> dict[str, int] | None:
        if not self.args.shard:
            return None
        index, total = parse_shard(self.args.shard)
        return {"index": index, "total": total}

    def _write_record(self) -> Judged:
        """Write ``story-run.json``, then judge the run directory as ``--judge`` does.

        The judged run's cost is what :meth:`_finish` adds to the final line.
        """
        artifacts.write_json(
            self.artifacts / story_verdict.RUN_FILE, self.run_record(), self.scrub
        )
        self.recorded = True
        judged = judge_run_dir(self.artifacts, self.runtime.environ)
        self.cost = story_verdict.cost_text(
            story_verdict.cost_totals(judged.results, judged.record.get("interactive"))
        )
        return judged

    def _record_after_failure(self) -> None:
        """After a failure or a cancel, the record of the flows that did run."""
        if self.recorded or self.ready is None or not self.attempts:
            return
        try:
            self._write_record()
        except Exception as error:
            self.say(f"the run's record could not be written: {type(error).__name__}")

    def _conclude(self) -> int:
        """Videos, the record, results and section, then the audits and the post."""
        if self.planned.mode != "watch":
            self._render_videos()
        judged = self._write_record()
        for line in flow_lines(judged.results):
            self.say(line)
        dirty = [
            attempt
            for attempt in self.attempts
            if attempt.context.audit_state == "violations" or attempt.context.strays
        ]
        if dirty:
            raise StoryRunError(
                "a flow's browser had a socket or a process it must not have: "
                + ", ".join(attempt.context.key for attempt in dirty[:5])
            )
        if self.planned.mode == "watch":
            return self._finish(
                "watched",
                f"judged without the socket audit, which the host folds in; see "
                f"{self.artifacts}",
            )
        if not self.args.post:
            return self._finish(
                "dry_run", f"nothing posted; section.md is in {self.artifacts}"
            )
        unclean = [
            attempt
            for attempt in self.attempts
            if attempt.opened and attempt.context.audit_state != "clean"
        ]
        if unclean or not any(attempt.opened for attempt in self.attempts):
            raise StoryRunError(
                "nothing is posted: a clean socket audit of every flow's browser is "
                f"the condition for posting, which needs Chrome to run as "
                f"{paths.BROWSER_USER} in a job running as root"
            )
        return self._post(judged)

    def _cleanup(self) -> None:
        """Stop what is still running: an attempt cut short, then kept recordings."""
        if self.current is not None:
            current, self.current = self.current, None
            current.cleanup()
        self._drop_kept()
        work, self.work_dir = self.work_dir, None
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)

    def _watch(self) -> None:
        """The one watched flow, in the browser container the host started.

        Watch has no dollar cap of its own (``--max-budget-usd`` applies only with
        ``--print``), so only the persona, the version and the time gates apply;
        the relay's caps and the wall clock bound the session. At exit the session
        file is read through ``ux_runner.transcript``, and the cost counted is the
        larger of the CLI's own total and its usage at the pinned rates. A session
        file that cannot be read records ``assisted`` as null, because whether
        anyone typed is then unknown rather than no.
        """
        if len(self.flows) != 1:
            raise StoryRunError("watch mode runs one flow: name the story and --as")
        attempt = self._new_attempt(self.flows[0], 1, 1)
        self.attempts.append(attempt)
        watch = WatchSession(self, attempt)
        self.current = watch
        try:
            watch.run_attempt()
        finally:
            self.current = None
            watch.cleanup()
            attempt.context = watch.context()
        attempt.result = judge_attempt(self.artifacts, attempt.context)
        self.say(f"flow {attempt.flow.key}: {_attempt_label(attempt.result)}")

    def _post(self, judged: Judged) -> int:
        """Reply once in this head's code-review thread, or post on its own.

        Only after the MR's head is confirmed unmoved and the token's notes hold no
        story reply for it (``gitlab_api.wait_for_review_note``), so a run that
        finds the head already replied to while it waited uploads nothing. The
        review note itself is never edited. Older story replies of the token's
        have their video lines marked superseded.
        """
        args, rt, plan = self.args, self.runtime, self.planned
        head = plan.head_sha
        credential = rt.environ.get(review.GITLAB.credential_env, "").strip()
        if not credential:
            raise review.Skip(_no_token_message())
        forge = _forge(args, rt, credential)
        deadline = (
            self.deadline
            if self.deadline is not None
            else rt.clock() + args.review_poll_minutes * 60
        )
        waited = gitlab_api.wait_for_review_note(
            forge,
            args.mr,
            head,
            already_posted=lambda mine: story_section.has_story_reply(mine, head),
            poll_minutes=args.review_poll_minutes,
            deadline=deadline,
            clock=rt.clock,
            sleep=rt.sleep,
            poll_seconds=_POLL_SECONDS,
        )
        if waited.outcome == "moved":
            moved = waited.moved_to or ""
            return self._finish(
                "superseded",
                f"the MR head moved to {moved[:8]} before posting, so nothing is "
                f"posted for {head[:8]}",
            )
        if waited.outcome == "posted":
            return self._finish("unchanged", "already run for this head")
        self._upload_videos(forge)
        judged = self._write_record()
        text = section_text(
            judged.record, judged.results, judged.found, _judge_scrub(rt.environ)
        )
        if text is None:
            raise StoryRunError("the run has no reply to post")
        where = "posted on its own"
        review_note = waited.review_note
        note_id = review_note.get("id") if review_note is not None else None
        thread = (
            forge.discussion_id_for(args.mr, note_id)
            if isinstance(note_id, int)
            else None
        )
        if thread is not None:
            forge.reply(args.mr, thread, text)
            where = "replied in the code review's thread"
        else:
            forge.create_note(args.mr, story_section.compose_standalone(text))
        self._supersede(forge, waited.mine, head)
        return self._finish("posted", f"first-time-user stories of {head[:8]} {where}")

    def _upload_videos(self, forge: gitlab_api.UxGitlab) -> None:
        """Upload each rendered video: its line and upload id, or a note saying why not.

        A refused or failed upload costs the reply that video and nothing else;
        the mp4 is in the job's artifacts either way.
        """
        limit = self.args.max_upload_mb * _MIB
        self.video_notes = [
            note for note in self.video_notes if note.get("kind") != "local"
        ]
        for attempt in sorted(
            self.attempts, key=lambda item: (item.context.key, item.number)
        ):
            name = attempt.context.video
            if name is None:
                continue
            key = attempt.context.key
            path = self.artifacts / name
            try:
                size = path.stat().st_size
            except OSError:
                self.video_notes.append(
                    {"key": key, "kind": "upload_failed", "detail": ""}
                )
                continue
            if size > limit:
                shown = f"{size / _MIB:.0f}"
                self.video_notes.append(
                    {"key": key, "kind": "over_limit", "detail": shown}
                )
                continue
            try:
                payload = forge.upload(path, filename=name)
                url = forge.checked_upload_url(payload)
            except (review.Skip, review.Failure) as error:
                self.say(f"upload: the video of {key} was not uploaded: {error}")
                self.video_notes.append(
                    {"key": key, "kind": "upload_failed", "detail": ""}
                )
                continue
            line = f"![UX story]({url})"
            ident = payload.get("id")
            if not story_section.STORY_VIDEO_LINE_RE.fullmatch(line) or not (
                isinstance(ident, int) and not isinstance(ident, bool)
            ):
                self.video_notes.append(
                    {"key": key, "kind": "upload_failed", "detail": ""}
                )
                continue
            self.video_lines.append(line)
            self.uploads.append(ident)

    def _supersede(
        self, forge: gitlab_api.UxGitlab, mine: list[dict[str, Any]], head: str
    ) -> list[int]:
        """Mark the video lines of up to ten older story replies of the token's superseded."""
        done: list[int] = []
        considered = 0
        for note in mine:
            if considered >= _SUPERSEDE_LIMIT:
                break
            ident = note.get("id")
            if not isinstance(ident, int) or isinstance(ident, bool):
                continue
            markers = story_section.story_markers([note])
            if not markers or markers[0].sha == head:
                continue
            considered += 1
            replaced = story_section.supersede_story_videos(_body(note), head[:8])
            if replaced is None:
                continue
            try:
                forge.update_note(self.args.mr, ident, replaced)
            except (review.Skip, review.Failure) as error:
                self.say(f"supersede: note {ident} not updated: {error}")
                continue
            done.append(ident)
        return done


def post_line(
    args: argparse.Namespace, runtime: Runtime, scrub: artifacts.Scrub, reply: str
) -> None:
    """Post a one-line story reply (unchanged, or the run allowance used up).

    In the head's code-review thread when that note exists now, else on its own;
    nothing is waited for, since nothing was run. A reply GitLab refuses is
    reported and changes no outcome.
    """
    marker = story_section.STORY_MARKER_RE.match(reply)
    credential = runtime.environ.get(review.GITLAB.credential_env, "").strip()
    if marker is None or not credential:
        return
    scrub.keep(reply.split("\n", 1)[0])
    text = scrub.note(reply)
    try:
        forge = _forge(args, runtime, credential)
        me = forge.current_user()["id"]
        notes = forge.notes(args.mr)
        if story_section.has_story_reply(
            gitlab_api.my_notes(notes, me), marker.group("sha")
        ):
            return
        review_note = gitlab_api.review_note_for(notes, marker.group("sha"), me)
        note_id = review_note.get("id") if review_note is not None else None
        thread = (
            forge.discussion_id_for(args.mr, note_id)
            if isinstance(note_id, int)
            else None
        )
        if thread is not None:
            forge.reply(args.mr, thread, text)
        else:
            forge.create_note(args.mr, story_section.compose_standalone(text))
    except (review.Skip, review.Failure) as error:
        _say(scrub, f"reply: not posted: {error}")


class WatchSession:
    """One watched attempt: the browser is the container's, the TUI is this run's.

    The container's ``browser.py`` started the proxy and Chrome and signed the
    persona in, and its own socket audit covers that Chrome's whole life, which the
    host folds into this run's record (``--judge --audit``) once it has stopped the
    container. This reads the signed-in page's facts over DevTools, closes that
    session, and runs the pinned Claude TUI with the same story server and tools
    as an unattended flow, under the catalogue's wall clock. The flow's prompt is
    the session's appended system prompt (``agent.interactive_argv``), and the
    person watching types ``agent.OPENING_MESSAGE`` once the server is up, which
    the transcript counts as the session's one message. No recording is made,
    because a watched run renders no video.
    """

    def __init__(self, run: StoryRun, attempt: Attempt) -> None:
        self.run = run
        self.attempt = attempt
        self.work: Path | None = None
        self.journal_copied: str | None = None
        self.not_run_reason: str | None = None
        self.harness_error: str | None = None

    def run_attempt(self) -> None:
        run, attempt = self.run, self.attempt
        flow, rt = attempt.flow, run.runtime
        try:
            rt.wait_for_cdp()
            target = rt.first_page_target()
            devtools = rt.open_session(target["webSocketDebuggerUrl"])
            try:
                facts = rt.page_facts(devtools)
            finally:
                devtools.close()
        except (chrome.ChromeError, OSError, KeyError) as error:
            self.harness_error = "launch"
            run.say(f"flow {flow.key}: harness error (launch): {error}")
            return
        attempt.facts = facts
        run.note_facts(facts)
        if stories.since_reached(flow.story.since, facts.version) is False:
            self.not_run_reason = "since"
            run.say(
                f"flow {flow.key}: not run, the stack is older than {flow.story.since}"
            )
            return
        base = "/tmp" if os.path.isdir("/tmp") else None  # nosec B108 - private dir
        work = Path(tempfile.mkdtemp(prefix="ux-test-stories-watch-", dir=base))
        self.work = work
        os.chmod(work, 0o700)
        try:
            argv, cwd, env, home = self._session(work)
        except (agent.AgentError, StoryRunError, OSError) as error:
            self.harness_error = "session"
            run.say(f"flow {flow.key}: harness error (session): {error}")
            return
        minutes = run.catalogue.limits.flow_minutes
        run.say(
            f"watch: {flow.key}, {attempt.budget} steps, at most {minutes} min. The "
            "brief and the task are the session's system prompt: once the session "
            "shows its prompt, give it a few seconds to start its ux-story server, "
            f"then type: {agent.OPENING_MESSAGE}"
        )
        began = rt.wall()
        completed = rt.run_tui(argv, cwd=cwd, env=env, timeout=minutes * 60)
        ended = rt.wall()
        session_file = transcript.read_transcript(home)
        pinned = pinned_cost(run.model, session_file.usage)
        known = [
            cost for cost in (session_file.cli_cost_usd, pinned) if cost is not None
        ]
        spent = round(max(known), 6) if known else None
        run.interactive = {
            **session_file.to_json(),
            "assisted": session_file.assisted if session_file.found else None,
            "cost_usd_pinned": pinned,
            "counted_usd": spent,
            "returncode": completed.returncode,
            "timed_out": completed.timed_out,
            "started_epoch": round(began, 3),
            "ended_epoch": round(ended, 3),
        }
        run.spent = round(run.spent + (spent or 0.0), 6)
        shown = f"${spent:.2f}" if spent is not None else "unknown"
        if not session_file.found:
            label = (
                "the transcript could not be read, so whether anyone typed is unknown"
            )
        else:
            label = "assisted" if session_file.assisted else "unassisted"
        run.say(f"watch: ended ({label}); cost {shown}")

    def _session(self, work: Path) -> tuple[list[str], Path, dict[str, str], Path]:
        run, attempt = self.run, self.attempt
        aws = run.claude_credentials(attempt)
        cwd = artifacts.private_dir(work / "cwd")
        home = artifacts.private_dir(work / "home")
        tmp = artifacts.private_dir(work / "tmp")
        start = run.write_start(work / "start.json", attempt, mode="watch")
        config = agent.write_mcp_config(
            work / "mcp.json",
            agent.story_server(
                toolchain=run.toolchain,
                story_adapter=paths.HARNESS_ROOT / STORY_SERVER_SCRIPT,
                index=run.index_file,
                out=work / "story",
                start=start,
                budget=attempt.budget,
                python=sys.executable,
                home=home,
                search_path=_search_path(),
            ),
        )
        prompt_file = work / "prompt.md"
        descriptor = os.open(
            prompt_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(run.prompt_for(attempt.flow, attempt.budget))
        argv = agent.interactive_argv(
            run.toolchain.claude,
            config,
            prompt_file=prompt_file,
            profile=tool_profiles.STORY,
            effort=run.effort,
        )
        env = agent.child_env(
            home=home,
            tmpdir=tmp,
            model=run.model,
            region=run.args.region,
            aws=aws,
            source=run.runtime.environ,
        )
        term = run.runtime.environ.get("TERM", "")
        if term:
            env["TERM"] = term
        return argv, cwd, env, home

    def cleanup(self) -> None:
        """Copy the journal and screens out, then remove the attempt's directory."""
        work, self.work = self.work, None
        if work is None:
            return
        try:
            folder = artifacts.artifact_dir(self.run.artifacts / self.attempt.folder)
            story_dir = work / "story"
            self.journal_copied = self.run.copy_journal(story_dir, folder, self.attempt)
            self.run.copy_screens(story_dir, folder, self.attempt)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def context(self) -> story_verdict.FlowContext:
        return dataclasses.replace(
            self.attempt.context,
            journal=self.journal_copied,
            not_run_reason=self.not_run_reason,
            harness_error=self.harness_error if self.not_run_reason is None else None,
        )


_CARD_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
_CARD_LT_RE = re.compile(r"&lt;(?!!--)")


def _card(text: object, limit: int) -> str:
    """One line for a video's end card: sanitised, then shown as GitLab shows it.

    The card is drawn as an image, so nothing on it is parsed; it has to stay on
    one line, which the recorder's parser keys on, and must not spell the
    recorder's own ``<!--`` chapter markers.
    """
    plain = section.sanitize(text, limit).replace(section.ZERO_WIDTH_SPACE, "")
    return _CARD_LT_RE.sub("<", _CARD_ESCAPE_RE.sub(r"\1", plain))


def _precheck_record(decision: Decision) -> dict[str, Any]:
    plan = decision.plan
    return {
        "version": 1,
        "proceed": decision.proceed,
        "status": decision.status,
        "detail": decision.detail,
        "mode": plan.mode if plan is not None else None,
        "head_sha": plan.head_sha if plan is not None else "",
        "flows": [str(key) for key in plan.keys] if plan is not None else [],
        **decision.extra,
    }


def _exit_code(status: str, args: argparse.Namespace) -> int:
    if status == "failed":
        return 1
    return 1 if status == "skipped" and args.fail_on_skip else 0


def precheck_main(args: argparse.Namespace, runtime: Runtime) -> int:
    """``--precheck``: decide, write the decision files, print one decision line.

    Before anything else the job's ``UX_HARNESS`` and ``UX_ENTRY`` must name
    exactly :data:`HARNESS_PATHS` and :data:`ENTRY` (``paths.harness_pin_problem``),
    because a project or pipeline variable overrides the job's own and the job
    extracts and runs what they say. Then a stale ``proceed`` is removed, so only
    this decision can leave one; ``inputs.json`` is written for a merge request,
    ``precheck.json`` always, and ``proceed`` only when the run should go ahead.
    The first line printed is ``PROCEED:``, ``SKIPPED:``, ``UNCHANGED:`` or
    ``FAILED:``.
    """
    scrub = artifacts.Scrub(artifacts.redactor_for(runtime.environ))
    problem = paths.harness_pin_problem(
        runtime.environ, harness_paths=HARNESS_PATHS, entry=ENTRY
    )
    if problem:
        _say(scrub, f"FAILED: {problem}")
        return 1
    try:
        out = artifacts.artifact_dir(Path(args.artifact_dir))
    except review.Failure as refused:
        _say(scrub, f"FAILED: {refused}")
        return 1
    try:
        (out / "proceed").unlink(missing_ok=True)
        decision = precheck(args, runtime, scrub)
        plan = decision.plan
        if plan is not None:
            scrub.keep(plan.head_sha)
            if plan.inputs is not None:
                write_inputs(plan.inputs, out / "inputs.json", scrub)
        artifacts.write_json(out / "precheck.json", _precheck_record(decision), scrub)
        for warning in decision.extra.get("warnings", []):
            _say(scrub, f"WARNING: {warning}")
        if decision.reply is not None and args.post:
            post_line(args, runtime, scrub, decision.reply)
        if decision.proceed and plan is not None:
            artifacts.write_artifact(out / "proceed", "")
            _say(scrub, f"PROCEED: {decision.detail} at {plan.head_sha[:8]}")
            return 0
        _say(scrub, f"{decision.status.upper()}: {decision.detail}")
        return _exit_code(decision.status, args)
    except Exception as error:
        with contextlib.suppress(OSError):
            (out / "proceed").unlink(missing_ok=True)
        reason = str(error) if isinstance(error, review.Failure) else ""
        _say(scrub, f"FAILED: {reason or _unexpected(scrub, error)}")
        return 1


def collect_main(args: argparse.Namespace, runtime: Runtime) -> int:
    """``--collect-only``: write the MR's StoryInputs for a run elsewhere, from git.

    The Docker runner runs this on the host, where git and SSH work, and hands the
    file to a container that mounts the repository read-only. API metadata is used
    when a token is set; an API that cannot be reached only costs the title.
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
        inputs = StoryInputs.from_json(
            runtime.collect_story(
                Path(args.repo_dir), args.mr, args.target_branch, remote=args.remote
            )
        )
        if merge_request is not None:
            inputs = _with_metadata(inputs, merge_request)
        write_inputs(inputs, target, scrub)
    except (mr_inputs.InputsError, review.Failure) as error:
        _say(scrub, f"FAILED: the MR could not be collected: {error}")
        return 1
    except Exception as error:
        _say(scrub, f"FAILED: {_unexpected(scrub, error)}")
        return 1
    _say(scrub, f"collected: {target}")
    return 0


_AUDIT_STATES = ("clean", "violations", "unavailable")
_AUDITED_MODES = ("watch", "pair")


def fold_audit(record: dict[str, Any], audit_path: Path) -> dict[str, Any]:
    """A watched or paired run's record with its browser container's socket audit.

    The container's audit covered its Chrome from launch until after exit, which
    is every moment the flow's page existed, so it is every flow's audit. A state
    outside the audit's own three reads as ``unavailable``, and only the number
    of strays is kept.
    """
    if record.get("mode") not in _AUDITED_MODES:
        raise StoryRunError(
            f"--audit folds a watched or paired run's browser audit, and this run "
            f"is {record.get('mode')!r}"
        )
    audit = _read_json(audit_path, "the browser audit", limit=_SUMMARY_MAX_BYTES)
    if not isinstance(audit, dict):
        raise StoryRunError("the browser audit is not a JSON object")
    state = audit.get("state")
    strays = audit.get("strays")
    folded = dict(record)
    folded["flows"] = [
        {
            **flow,
            "audit_state": state if state in _AUDIT_STATES else "unavailable",
            "strays": len(strays) if isinstance(strays, list) else 0,
        }
        for flow in record.get("flows") or []
        if isinstance(flow, dict)
    ]
    return folded


def judge_main(args: argparse.Namespace, runtime: Runtime) -> int:
    """``--judge``: rewrite, or with ``--check`` compare, a run's two outputs.

    Exit 0; 1 for a record that cannot be read or a ``--check`` difference, each
    difference printed as ``different``, the file and where; 3, writing and
    comparing nothing, for a run judged under another ``JUDGEMENT_REVISION``,
    unless ``--rejudge`` asks for it to be judged again under this one.
    ``--summary`` prints the structured table and nothing else of the run.
    Standard library only, and it never runs bash, so it works on any host.
    """
    run_dir = Path(args.judge)
    try:
        record = _read_json(run_dir / story_verdict.RUN_FILE, story_verdict.RUN_FILE)
        if not isinstance(record, dict):
            raise story_verdict.RunRecordError("story-run.json is not a JSON object")
        revision = record.get("judgement_revision")
        if _is_whole(revision) and revision != story_verdict.JUDGEMENT_REVISION:
            print(
                f"judgement_revision\trun={revision}\t"
                f"judge={story_verdict.JUDGEMENT_REVISION}"
            )
            if not args.rejudge:
                if not args.check:
                    print(story_verdict.other_revision_line(revision))
                return 3
            print(
                f"WARNING: re-judged under JUDGEMENT_REVISION "
                f"{story_verdict.JUDGEMENT_REVISION}; the run was judged under {revision}"
            )
        if args.audit is not None:
            folded = fold_audit(record, Path(args.audit))
            artifacts.write_artifact(
                run_dir / story_verdict.RUN_FILE,
                json.dumps(folded, indent=2, ensure_ascii=False) + "\n",
            )
        judged = judge_run_dir(run_dir, runtime.environ, check=args.check)
    except (story_verdict.RunRecordError, StoryRunError, review.Failure) as error:
        print(f"FAILED: {error}")
        return 1
    for name, where in judged.differences:
        print(f"different\t{name}\t{where}")
    if args.summary:
        sys.stdout.write(
            story_verdict.summary_table(run_dir, judged.results, judged.found)
        )
    return 1 if judged.differences else 0


def _shard_order(path: Path) -> tuple[int, int, str]:
    found = re.search(r"([0-9]+)$", path.name)
    return (0, int(found.group(1)), path.name) if found else (1, 0, path.name)


def shard_dirs(roots: Sequence[Path]) -> list[Path]:
    """Each root that holds a ``story-run.json``, else its subdirectories that do.

    Subdirectories are taken in shard order (``shard-2`` before ``shard-10``), and
    a link is never followed into.
    """
    found: list[Path] = []
    for root in roots:
        if root.is_symlink():
            continue
        if (root / story_verdict.RUN_FILE).is_file():
            found.append(root)
            continue
        try:
            children = [
                child
                for child in root.iterdir()
                if child.is_dir()
                and not child.is_symlink()
                and (child / story_verdict.RUN_FILE).is_file()
            ]
        except OSError:
            continue
        found += sorted(children, key=_shard_order)
    unique: list[Path] = []
    for path in found:
        if path.resolve() not in {item.resolve() for item in unique}:
            unique.append(path)
    return unique


def merge_records(parts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The shards' ``story-run.json`` as one, by ``story_digest.merge_runs``.

    Then what that leaves to the first shard is made order-free: the review-stack
    flag is the run's own rule over every shard's opened flows, false when any
    shard's is, true when every shard that opened a browser says true, and null
    when none opened one, whichever shard comes first; the withheld
    flows are every shard's, in key order, and the reason quoted first is the
    first of those. The merged reply's ``sel`` covers every flow and its notes
    every shard's, and its ``chosen_by`` is every shard's choice as one run's
    (:func:`_merged_chosen_by`). The merged confinement is ``full`` only when every
    shard's is, ``dev-unconfined`` when any shard's is, and null otherwise.
    """
    merged = story_digest.merge_runs(parts)
    confinements = {part.get("confinement") for part in parts}
    if confinements == {"full"}:
        merged["confinement"] = "full"
    elif "dev-unconfined" in confinements:
        merged["confinement"] = "dev-unconfined"
    else:
        merged["confinement"] = None
    flags = []
    for part in parts:
        stack = part.get("stack")
        flags.append(stack.get("review_stack") if isinstance(stack, Mapping) else None)
    known = [flag for flag in flags if flag is not None]
    stack = dict(merged.get("stack") or {})
    stack["review_stack"] = all(flag is True for flag in known) if known else None
    merged["stack"] = stack
    withheld = sorted(
        (
            dict(item)
            for part in parts
            for item in (part.get("video_withheld_flows") or [])
            if isinstance(item, Mapping)
        ),
        key=lambda item: (str(item.get("key")), str(item.get("attempt"))),
    )
    merged["video_withheld_flows"] = withheld
    merged["video_withheld"] = withheld[0].get("reason") if withheld else None
    replies: list[Mapping[str, Any]] = [
        reply for part in parts if isinstance(reply := part.get("reply"), Mapping)
    ]
    if replies:
        reply = dict(replies[0])
        reply["video_notes"] = [
            note for item in replies for note in (item.get("video_notes") or [])
        ]
        reply["uploads"] = []
        reply["videos"] = []
        reply["sel"] = _merged_sel(merged, reply)
        reply["chosen_by"] = _merged_chosen_by(replies)
        merged["reply"] = reply
    return merged


def _merged_chosen_by(replies: Sequence[Mapping[str, Any]]) -> object:
    """How the shards' flows were chosen, as one run's choice.

    Shards of one run share a kind of choice. Hand-picked shards name every
    shard's flows, in key order, and shards of a set are that whole set, with no
    shard named. Any other kind, shards that differ in kind, and a shard whose
    record predates ``chosen_by``, leave the first shard's as it is.
    """
    choices = [reply.get("chosen_by") for reply in replies]
    first = choices[0]
    if not all(isinstance(choice, Mapping) for choice in choices):
        return first
    kinds = {choice.get("kind") for choice in choices if isinstance(choice, Mapping)}
    if kinds == {"story"}:
        flows = {
            flow
            for choice in choices
            if isinstance(choice, Mapping)
            for flow in choice.get("flows") or []
            if isinstance(flow, str)
        }
        return {"kind": "story", "flows": sorted(flows)}
    if kinds == {"all"}:
        tiers = list(
            dict.fromkeys(
                str(choice.get("tier") or "p0")
                for choice in choices
                if isinstance(choice, Mapping)
            )
        )
        return {"kind": "all", "tier": " and ".join(tiers), "shard": None}
    return first


def _merged_sel(merged: Mapping[str, Any], reply: Mapping[str, Any]) -> str | None:
    """The ``sel`` of every merged flow's first attempt, with the first reply's UI
    source and docs tree; the first reply's own ``sel`` when those are absent."""
    pairs: list[tuple[stories.FlowKey, str]] = []
    for flow in merged.get("flows") or []:
        if isinstance(flow, Mapping) and flow.get("attempt") == 1:
            pairs.append((stories.FlowKey.parse(str(flow["key"])), str(flow["series"])))
    try:
        return changelog_select.selection_hash(
            pairs,
            ui_source=str(reply.get("ui_source")),
            docs_tree=str(reply.get("docs_tree")),
        )
    except ValueError:
        sel = reply.get("sel")
        return sel if isinstance(sel, str) else None


def _copy_tree(source: Path, target: Path, *, screens: bool = True) -> None:
    """Copy the regular files under ``source`` to ``target``; links are left out,
    and so are the screenshot folders when ``screens`` is False."""
    for current, names, files in os.walk(source, followlinks=False):
        here = Path(current)
        relative = here.relative_to(source)
        artifacts.artifact_dir(target / relative)
        names[:] = [
            name
            for name in names
            if not (here / name).is_symlink() and (screens or name != _SCREENS_DIR)
        ]
        for name in files:
            path = here / name
            if path.is_symlink() or not path.is_file():
                continue
            with (
                path.open("rb") as handle,
                artifacts.open_artifact(target / relative / name) as out,
            ):
                shutil.copyfileobj(handle, out)


def merge_dirs(
    dirs: Sequence[Path],
    out: Path,
    environ: Mapping[str, str],
    *,
    media: bool = True,
) -> Judged:
    """Several run directories as one in ``out``, judged there as ``--judge`` would.

    The record is :func:`merge_records`'s and the journals are copied, and with
    ``media`` the screens and videos too, so ``out`` is a run directory like any
    other; the digest leaves them in the shards' own artifacts. Its
    ``results.json`` is judged from the copied journals, and is checked against
    ``story_digest.merge_results`` over the shards' own, which a ``WARNING:`` line
    reports when they differ or cannot be compared, as when two shards ran
    harnesses of different judgement revisions.
    """
    runs = [
        _read_json(path / story_verdict.RUN_FILE, f"{path.name}'s story-run.json")
        for path in dirs
    ]
    results = [
        _read_json(path / story_verdict.RESULTS_FILE, f"{path.name}'s results.json")
        for path in dirs
    ]
    merged = merge_records(runs)
    out = artifacts.artifact_dir(out)
    for path in dirs:
        flows = path / story_verdict.FLOWS_DIR
        if flows.is_dir() and not flows.is_symlink():
            _copy_tree(flows, out / story_verdict.FLOWS_DIR, screens=media)
        for video in sorted(path.glob("*.mp4")) if media else ():
            if video.is_file() and not video.is_symlink():
                with (
                    video.open("rb") as handle,
                    artifacts.open_artifact(out / video.name) as target,
                ):
                    shutil.copyfileobj(handle, target)
    artifacts.write_artifact(
        out / story_verdict.RUN_FILE,
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
    )
    judged = judge_run_dir(out, environ)
    try:
        expected: object = story_digest.merge_results(results)
    except story_verdict.RunRecordError as error:
        expected = None
        print(
            f"WARNING: the shards' own results cannot be compared ({error}); "
            "results.json is their journals' as this harness judges them",
            flush=True,
        )
    written = _read_json(out / story_verdict.RESULTS_FILE, "the merged results.json")
    if expected is not None and json.loads(json.dumps(expected)) != written:
        print(
            "WARNING: the shards' own results differ from what their journals judge "
            "to; results.json is the journals'",
            flush=True,
        )
    return judged


def merge_main(args: argparse.Namespace, runtime: Runtime) -> int:
    """``--merge DIR... --out DIR``: several shards' runs as one run directory."""
    scrub = artifacts.Scrub(artifacts.redactor_for(runtime.environ))
    dirs = shard_dirs([Path(path) for path in args.merge])
    if not dirs:
        _say(scrub, "FAILED: no story-run.json under " + ", ".join(args.merge))
        return 1
    try:
        judged = merge_dirs(dirs, Path(args.out), runtime.environ)
    except (story_verdict.RunRecordError, StoryRunError, review.Failure) as error:
        _say(scrub, f"FAILED: {error}")
        return 1
    for line in flow_lines(judged.results):
        _say(scrub, line)
    cost = story_verdict.cost_text(story_verdict.cost_totals(judged.results))
    _say(scrub, f"MERGED: {len(dirs)} run(s) into {args.out}; {cost}")
    return 0


def _night(
    results: Mapping[str, Any], run: Mapping[str, Any], environ: Mapping[str, str]
) -> story_digest.Night:
    pipeline = environ.get("CI_PIPELINE_ID", "").strip()
    pipeline_id = int(pipeline) if pipeline.isdigit() else 0
    commit = environ.get("CI_COMMIT_SHA", "").strip()
    if not _SHA_RE.fullmatch(commit):
        harness = run.get("harness_sha")
        commit = (
            harness
            if isinstance(harness, str) and _SHA_RE.fullmatch(harness)
            else "0" * 40
        )
    return story_digest.night_from_results(
        results, run, pipeline_id=pipeline_id, develop_sha=commit
    )


def _history_record(history: story_digest.History, available: bool) -> dict[str, Any]:
    return {
        "available": available,
        "gaps": list(history.gaps),
        "nights": [
            {
                "pipeline_id": night.pipeline_id,
                "develop_sha": night.develop_sha,
                "stack_state": night.stack_state,
                "comparable": night.comparable,
                "audits_clean": night.audits_clean,
                "flows": [dataclasses.asdict(flow) for flow in night.flows],
            }
            for night in history.nights
        ],
    }


def digest_main(args: argparse.Namespace, runtime: Runtime) -> int:
    """``--digest DIR --out DIR``: the shards merged, compared with earlier nights.

    It runs no model and no browser and imports nothing outside the standard
    library. The earlier nights come from ``runtime.history``
    (:func:`nightly_history`); without them nothing is compared, and ``digest.md``
    says so. ``digest.json`` is what later nights read back; ``history.json`` is
    the nights it was compared with. Once those are written, a scheduled pipeline
    with a webhook posts the one-line Slack summary (:func:`post_digest_summary`),
    which never changes the outcome.
    """
    scrub = artifacts.Scrub(artifacts.redactor_for(runtime.environ))
    dirs = shard_dirs([Path(args.digest)])
    out = Path(args.out)
    if not dirs:
        return idle_digest_main(args, runtime, scrub, out)
    try:
        judged = merge_dirs(dirs, out, runtime.environ, media=False)
        results = _read_json(
            out / story_verdict.RESULTS_FILE, "the merged results.json"
        )
        tonight = _night(results, judged.record, runtime.environ)
        history = runtime.history(args, runtime.environ) if runtime.history else None
        available = history is not None
        known = history if history is not None else story_digest.History((), ())
        found = story_digest.regressions(
            tonight,
            known,
            rerun_did_not_fit={
                str(item.get("key"))
                for item in judged.record.get("reruns_not_run") or []
                if isinstance(item, Mapping)
            },
        )
        fixed = story_digest.fixed(tonight, known)
        spend = judged.record.get("spend_usd")
        digest = story_digest.digest_document(
            tonight,
            known,
            found,
            fixed,
            spend_usd=float(spend) if isinstance(spend, (int, float)) else 0.0,
            injections=sum(result.notes.injections for result in judged.results),
            shards_ran=len(dirs),
            reruns_not_run=len(judged.record.get("reruns_not_run") or []),
        )
        catalogue = load_catalogue()
        nudges = _unreleased_nudges(Path(args.repo_dir), catalogue)
        cap = len(dirs) * float(catalogue.runs.job_usd)
        text = story_digest.compose_digest_md(
            digest,
            results,
            judged.record,
            uncovered=nudges,
            cap_usd=cap,
            history=history,
        )
        if not available:
            text = text.rstrip("\n") + "\n\n_History unavailable: nothing compared._\n"
        artifacts.write_artifact(
            out / "digest.json", json.dumps(digest, indent=2, ensure_ascii=False) + "\n"
        )
        artifacts.write_artifact(out / "digest.md", scrub.note(text))
        artifacts.write_json(
            out / "history.json", _history_record(known, available), scrub
        )
        cost = story_verdict.cost_text(story_verdict.cost_totals(judged.results))
    except (
        story_verdict.RunRecordError,
        StoryRunError,
        review.Failure,
        ValueError,
    ) as error:
        _say(scrub, f"FAILED: {error}")
        return 1
    post_digest_summary(digest, runtime, scrub)
    _say(
        scrub,
        f"DIGEST: {len(dirs)} shard(s), {len(found)} regression(s), in {out}; {cost}",
    )
    return 0


def _skipped_shards(root: Path) -> list[tuple[str, str, str]]:
    """Each shard directory under ``root`` with the status and detail of its precheck."""
    found: list[tuple[str, str, str]] = []
    try:
        children = sorted(
            (child for child in root.iterdir() if child.is_dir()),
            key=_shard_order,
        )
    except OSError:
        return found
    for child in children:
        if child.is_symlink():
            continue
        try:
            record = _read_json(child / "precheck.json", f"{child.name}'s precheck")
        except StoryRunError:
            found.append((child.name, "unknown", "it left no readable precheck.json"))
            continue
        status = record.get("status") if isinstance(record, dict) else None
        detail = record.get("detail") if isinstance(record, dict) else None
        found.append((child.name, str(status or "unknown"), str(detail or "")))
    return found


def idle_digest_main(
    args: argparse.Namespace, runtime: Runtime, scrub: artifacts.Scrub, out: Path
) -> int:
    """The digest of a night on which no shard ran: recorded, never compared.

    Its ``digest.json`` is one a later night reads as a gap, and its ``digest.md``
    lists each shard's precheck. A scheduled pipeline posts its Slack line too.
    """
    try:
        history = runtime.history(args, runtime.environ) if runtime.history else None
        known = history if history is not None else story_digest.History((), ())
        pipeline = runtime.environ.get("CI_PIPELINE_ID", "").strip()
        commit = runtime.environ.get("CI_COMMIT_SHA", "").strip()
        digest = story_digest.idle_digest_document(
            pipeline_id=int(pipeline) if pipeline.isdigit() else 0,
            develop_sha=commit if _SHA_RE.fullmatch(commit) else "0" * 40,
            history=known,
        )
        text = story_digest.compose_idle_digest_md(
            digest, _skipped_shards(Path(args.digest))
        )
        out = artifacts.artifact_dir(out)
        artifacts.write_artifact(
            out / "digest.json", json.dumps(digest, indent=2, ensure_ascii=False) + "\n"
        )
        artifacts.write_artifact(out / "digest.md", scrub.note(text))
        artifacts.write_json(
            out / "history.json", _history_record(known, history is not None), scrub
        )
    except (StoryRunError, review.Failure, ValueError) as error:
        _say(scrub, f"FAILED: {error}")
        return 1
    post_digest_summary(digest, runtime, scrub)
    _say(scrub, f"DIGEST: no shard ran under {args.digest}; recorded in {out}")
    return 0


def _unreleased_nudges(repo_dir: Path, catalogue: stories.Catalogue) -> list[str]:
    """The ``[Unreleased]`` CHANGELOG leads no story covers, from ``repo_dir``."""
    try:
        text = _read_bounded(
            repo_dir / "CHANGELOG.md", ui_change.MAX_OUTPUT_BYTES, "CHANGELOG.md"
        ).decode("utf-8", errors="replace")
    except StoryRunError:
        return []
    return changelog_select.uncovered_unreleased(catalogue, text)


def run(args: argparse.Namespace, runtime: Runtime) -> int:
    """A run of planned flows: precheck, confinement, sessions, judging, the reply."""
    return StoryRun(args, runtime).execute()


def _positive_float(text: str) -> float:
    value = float(text)
    if not (math.isfinite(value) and value > 0):
        raise argparse.ArgumentTypeError("must be a positive, finite amount")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--story", metavar="ID", help="run this story's flows")
    modes.add_argument("--all", action="store_true", help="run the nightly set")
    modes.add_argument("--mr", type=int, metavar="IID", help="run what this MR selects")
    modes.add_argument("--replay", metavar="RUN_DIR", help="run an earlier run's flows")
    modes.add_argument("--judge", metavar="RUN_DIR", help="re-judge a run directory")
    modes.add_argument(
        "--merge", nargs="+", metavar="DIR", help="merge run directories into --out"
    )
    modes.add_argument(
        "--digest", metavar="DIR", help="the nightly digest of DIR's shards into --out"
    )
    parser.add_argument(
        "--as",
        dest="as_persona",
        metavar="PERSONA",
        help="with --story, only this persona",
    )
    parser.add_argument(
        "--tier",
        choices=(*stories.TIERS, "all"),
        default="p0",
        help="with --all, the tier to run (default: p0)",
    )
    parser.add_argument(
        "--shard", metavar="I/N", help="run only shard I of N, by balanced round robin"
    )
    steps = parser.add_mutually_exclusive_group()
    steps.add_argument(
        "--precheck",
        action="store_true",
        help="decide on a bare python3, write precheck.json and proceed, then stop",
    )
    steps.add_argument(
        "--collect-only",
        action="store_true",
        help="with --mr, write the MR's inputs.json from git, then stop",
    )
    steps.add_argument(
        "--interactive",
        action="store_true",
        help="with --story and --as, watch the flow in the browser container's TUI",
    )
    parser.add_argument("--inputs", type=Path, help="the MR's inputs.json")
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=Path(DEFAULT_RUN_DIR_NAME),
        help=f"where the run's records go (default: {DEFAULT_RUN_DIR_NAME})",
    )
    parser.add_argument("--out", type=Path, help="with --merge or --digest, the output")
    parser.add_argument(
        "--check", action="store_true", help="with --judge, write nothing and compare"
    )
    parser.add_argument(
        "--summary", action="store_true", help="with --judge, print the summary table"
    )
    parser.add_argument(
        "--audit",
        type=Path,
        help="with --judge, a watched or paired run's browser-audit.json",
    )
    parser.add_argument(
        "--rejudge",
        action="store_true",
        help="with --judge, judge a run judged under another JUDGEMENT_REVISION "
        "again under this one, rewriting its results",
    )
    parser.add_argument(
        "--repo-dir",
        type=Path,
        default=Path.cwd(),
        help="the checkout git and the docs are read from (default: here)",
    )
    parser.add_argument(
        "--build-dir",
        type=Path,
        help="a UI build to test (an MR run's default: <repo-dir>/src/ui/build)",
    )
    parser.add_argument("--web-url", help="the deployed stack's web URL")
    parser.add_argument(
        "--docs",
        metavar="REF",
        help="read the docs from this commit (default: the working tree locally)",
    )
    parser.add_argument(
        "--stack",
        help=f"the stack (default: {session.REVIEW_STACK}, the one whose video is "
        "made; with --replay, the replayed run's)",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL,
        help=f"Bedrock model id (default: $ANTHROPIC_MODEL or {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--effort",
        choices=agent.EFFORT_LEVELS,
        default=DEFAULT_EFFORT,
        help=f"the pinned effort level (default: {DEFAULT_EFFORT})",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or _DEFAULT_REGION,
        help=f"region of Bedrock, STS and Polly (default: $AWS_REGION or {_DEFAULT_REGION})",
    )
    parser.add_argument(
        "--target-branch",
        default=review.DEFAULT_TARGET_BRANCH,
        help=f"the MR's target branch (default: {review.DEFAULT_TARGET_BRANCH})",
    )
    parser.add_argument(
        "--remote", default="origin", help="git remote (default: origin)"
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
        help="GitLab API base URL (default: $CI_API_V4_URL)",
    )
    parser.add_argument(
        "--deploy-summary",
        type=Path,
        help="the nightly deploy's deployment_summary.txt, for the stack's state",
    )
    parser.add_argument(
        "--deadline-minutes",
        type=int,
        help="minutes after the job started by which the run must be done "
        "(default: the catalogue's in CI, none locally)",
    )
    parser.add_argument(
        "--run-usd",
        type=_positive_float,
        help="the run's dollar cap (default: the catalogue's job_usd in CI, else "
        "flows times flow_usd); not read by --interactive",
    )
    parser.add_argument(
        "--reruns",
        type=int,
        choices=(0, 1),
        help="re-run a bad verdict once (1, the default) or never (0); not read by "
        "--interactive",
    )
    parser.add_argument(
        "--video",
        choices=VIDEO_POLICIES,
        default="findings",
        help="which flows get a video, of the review stack only (default: findings)",
    )
    parser.add_argument("--voice", default="Ruth", help="Amazon Polly voice")
    parser.add_argument(
        "--no-narration", action="store_true", help="render videos without a voice"
    )
    parser.add_argument(
        "--narrate-with-own-credentials",
        action="store_true",
        help=(
            "when no Polly-only session can be minted, narrate with this run's own "
            "AWS credentials rather than render without a voice; refused wherever "
            "CI or GITLAB_CI is set"
        ),
    )
    parser.add_argument(
        "--max-upload-mb",
        type=int,
        default=95,
        help="the largest video uploaded, in MiB (default: 95)",
    )
    parser.add_argument(
        "--review-poll-minutes",
        type=int,
        default=10,
        help="how long to wait for the code-review note before posting on its own",
    )
    posting = parser.add_mutually_exclusive_group()
    posting.add_argument(
        "--post",
        action="store_true",
        help="with --mr, reply on the MR (only against the review stack)",
    )
    posting.add_argument(
        "--dry-run",
        action="store_true",
        help="write section.md and post nothing, which is the default",
    )
    parser.add_argument(
        "--fail-on-skip", action="store_true", help="exit 1 instead of 0 on a skip"
    )
    parser.add_argument(
        "--dev-unconfined",
        action="store_true",
        help=(
            "development only, refused under CI: no managed policy or uid switch; "
            "never posts"
        ),
    )
    parser.add_argument("--toolchain-prefix", help="where install.sh put the toolchain")
    parser.add_argument(
        "--keep-run-dir", action="store_true", help="keep each flow's run directory"
    )
    args = parser.parse_args(argv)
    if args.stack is None and args.replay is None:
        args.stack = session.REVIEW_STACK
    if args.as_persona is not None and args.story is None:
        parser.error("--as needs --story")
    if args.shard is not None:
        try:
            parse_shard(args.shard)
        except ValueError as error:
            parser.error(str(error))
        if args.mr is not None or args.interactive:
            parser.error("--shard splits --all, --story or --replay runs")
    if args.interactive and (args.story is None or args.as_persona is None):
        parser.error("--interactive runs one flow: give --story and --as")
    if args.interactive and (args.run_usd is not None or args.reruns is not None):
        parser.error(
            "--interactive reads neither --run-usd nor --reruns: a watched flow is "
            "bounded by its step budget and wall clock, and is never re-run"
        )
    if args.reruns is None:
        args.reruns = 1
    if args.collect_only and args.mr is None:
        parser.error("--collect-only needs --mr")
    if args.post and args.mr is None:
        parser.error("--post replies on a merge request: it needs --mr")
    if args.post and args.dev_unconfined:
        parser.error("--dev-unconfined never posts")
    if (args.merge or args.digest) and args.out is None:
        parser.error("--merge and --digest need --out")
    if args.audit is not None and args.judge is None:
        parser.error("--audit needs --judge")
    if (args.check or args.summary or args.rejudge) and args.judge is None:
        parser.error("--check, --summary and --rejudge need --judge")
    if args.check and args.rejudge:
        parser.error("--check writes nothing, so --rejudge has nothing to rewrite")
    if args.max_upload_mb < 1 or args.review_poll_minutes < 0:
        parser.error(
            "--max-upload-mb is at least 1 and --review-poll-minutes at least 0"
        )
    if args.deadline_minutes is not None and args.deadline_minutes < 1:
        parser.error("--deadline-minutes must be at least 1")
    if args.mr is not None:
        if args.mr < 1:
            parser.error("--mr must be a positive iid")
        if args.build_dir is None:
            args.build_dir = Path(args.repo_dir) / "src" / "ui" / "build"
    return args


#: The umask every file and directory a run makes is created under, whatever the
#: caller's: a GitLab Docker executor runs a job at umask 0000 by default.
_UMASK = 0o022


def main(argv: list[str] | None = None, runtime: Runtime | None = None) -> int:
    """Set the umask and the SIGTERM handler, then dispatch to the mode.

    Under the umask a GitLab Docker executor leaves a job, every directory a run
    makes would be writable by the browser's and the proxy's uids. Both are put
    back on the way out, so a caller that imports this module keeps its own.
    ``--post`` is refused against any stack but the review stack before anything
    runs, and ``--dev-unconfined`` wherever GitLab CI's own ``CI`` or
    ``GITLAB_CI`` is set, read from the runtime's environment, so a pipeline's
    flows always run under the whole confinement.
    """
    previous_umask = os.umask(_UMASK)
    restore = processes.install_sigterm_handler()
    try:
        args = parse_args(argv)
        chosen = runtime if runtime is not None else default_runtime()
        ci = _ci_variable(chosen.environ)
        if args.dev_unconfined and ci:
            print(
                "FAILED: --dev-unconfined is for a development host's own Chrome, "
                f"and {ci} is set, so this is CI",
                flush=True,
            )
            return 1
        if args.narrate_with_own_credentials and ci:
            print(
                "FAILED: --narrate-with-own-credentials is for a developer's own "
                f"machine, and {ci} is set, so this is CI",
                flush=True,
            )
            return 1
        if args.post and args.stack != session.REVIEW_STACK:
            print(
                f"FAILED: --post replies only for runs against the review stack "
                f"{session.REVIEW_STACK}",
                flush=True,
            )
            return 1
        if args.judge is not None:
            return judge_main(args, chosen)
        if args.merge:
            return merge_main(args, chosen)
        if args.digest is not None:
            return digest_main(args, chosen)
        if args.collect_only:
            return collect_main(args, chosen)
        if args.precheck:
            return precheck_main(args, chosen)
        return run(args, chosen)
    except (processes.Terminated, KeyboardInterrupt):
        print("FAILED: cancelled", flush=True)
        return 1
    finally:
        restore()
        os.umask(previous_umask)


if __name__ == "__main__":
    sys.exit(main())
