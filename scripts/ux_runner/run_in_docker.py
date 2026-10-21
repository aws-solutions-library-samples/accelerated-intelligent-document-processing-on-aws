#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The MR UX test on a laptop or devbox — the CI job's toolchain, in Docker.

This is the local form of the ``ai_ux_test`` CI job. It builds (or reuses) the
image ``scripts/ux_runner/Dockerfile`` builds from ``scripts/ux_runner/install.sh``,
which is the same toolchain definition the CI job installs, so a run on a Mac
(Apple Silicon included), on Windows under Docker Desktop or on a Linux devbox
gets the same Chrome for Testing, Node, npm pins, ffmpeg, fonts, Python pins,
managed policy and users as CI does. It has three modes.

``review``
    The CI-equivalent test of one merge request, a **dry run** unless
    ``--post``. The MR is collected on the host, its ``src/ui`` is built in a
    container that holds no credentials, and the test runs in a second container.
    ``section.md``, ``report.md``, ``summary.json`` and the mp4 land in
    ``scratch/ux-docker/mr-<iid>/ux-tests/``, which is gitignored; against a
    ``--stack`` other than the review stack there is no mp4 unless
    ``--dev-video-any-stack`` is passed through (``ai_ux_test.py``'s property 6).
    That opt-in makes the run a dry run even with ``--post``, and the host's
    ``CI`` and ``GITLAB_CI`` reach the second container, so on a CI host it is
    refused there as it is in the CI job.
``browser``
    Only the confined headless Chrome, the egress proxy and the recorder, with
    DevTools published on the host's own loopback (``127.0.0.1:9222``), for an
    interactive ux-test or product-demo session on a machine with no local
    Chrome. ``--stop`` stops it and leaves the socket audit behind.
``stories``
    First-time-user stories from ``scripts/ux_stories.toml``, run by
    ``scripts/sdlc/ai_ux_test_stories.py`` in the same image. Every run gets its
    own ``scratch/ux-docker/stories/<UTC time>-<run>/``, whose
    ``ux-test-stories/`` holds ``story-run.json``, ``results.json``,
    ``flows/<story@persona>/`` and ``section.md``, and the terminal ends with one
    line per flow: its verdict, the steps used of its budget and its
    hesitations. By default a run is unattended and a **dry run** unless
    ``--post``: one flow (``--story --as``), every persona of a story
    (``--story``), what an MR's job would select (``--mr``, collected on the
    host, its ``src/ui`` built in the credential-free container when the MR
    changes it) or the nightly set (``--all``), in ``--jobs`` containers with a
    network namespace each, whose shards ``--merge`` joins. ``--interactive``
    starts a fresh browser container, runs the pinned Claude TUI in it for you to
    watch and type to, stops the container when the session ends or
    ``flow_minutes`` pass, and judges the run. ``--pair`` starts the browser and
    prints the command for a session of your own (below). ``--replay`` re-runs a
    run from its own harness commit. ``--list`` and ``--search`` are the
    authoring helpers; ``--search`` runs in the image, because the docs index runs
    ``docs-site/setup.sh``.

The pair session
----------------
``--pair`` writes ``<run>/pair-mcp.json`` (mode 0600), whose one server is
``ux-story`` in relay mode, started as ``docker exec -i <browser>
/usr/bin/env -i ...``. The browser container holds the personas and
``UX_TEST_UI_ENV``, for ``browser.py``, and no AWS session; the server's ``env -i``
leaves it neither. The target prints the ``claude`` command and the message to
open the session with. The command passes the brief and the task itself, as
``--append-system-prompt-file <run>/pair-prompt.md``, because a session with no
built-in tools has no Read or Skill tool to open the skill with, and the message is
typed once the session has started its server: at 2.1.281 a message given as the
TUI's argument is answered before the server's tools are offered.

The session starts in a fresh, empty directory outside the checkout. Claude Code
reads ``CLAUDE.md`` from its working directory and every parent and keys its
auto-memory by that directory, so neither this repository's ``CLAUDE.md`` nor the
memory it keeps for this project reaches the first-time user. It also starts
with ``--bare``, which skips every ``CLAUDE.md``, auto-memory, hook and plugin,
the user-level ones included, and ``--strict-mcp-config`` and ``--tools=``, so the
story tools are the only ones it holds. ``--bare`` still applies the ``env`` of the
user's ``settings.json``, so a Bedrock or API-key setup kept there works; what it
costs is the sign-in it reads no keychain for, so a ``claude`` signed in only
through the keychain is not signed in. ``--pair-keychain`` drops ``--bare`` for
that ``claude``, and then the user-level ``CLAUDE.md``, hooks and plugins load as
well. ``--pair-full`` is your whole session, started from the checkout, for trusted
UI and docs only. ``--tools=`` rather than ``--tools ''`` keeps the list empty on
every shell, Windows PowerShell 5.1 included, which drops an empty argument; the
recordings in ``scripts/tests/fixtures/ux_story_transcript/`` show what these
flags sent at the pin.

Files the containers write
--------------------------
A container writes as root, and on a Linux host whose Docker is neither rootless
nor remapped those files stay root's, so the run could not be re-judged on the
host. After each container, anything under the run directory owned by another
user is handed back by a container with no network and no credentials
(``chown -hR``). A pair session's server instead runs as the developer
(``docker exec --user``) where a probe file shows the container's root to be
foreign, because nothing of this script runs when that session ends.

Git inside a story container
----------------------------
The orchestrator reads a commit's docs, an MR head's or ``--docs``', from git
objects in its ``--repo-dir``. Git refuses a repository another user owns, which a
bind-mounted checkout is to root on Linux, so each container that runs the
orchestrator gets ``safe.directory`` as command-line configuration
(``GIT_CONFIG_COUNT``), and a worktree checkout, whose ``.git`` file names its
shared git directory by host path, gets that directory mounted read-only at the
same path. ``UX_HARNESS_SHA`` carries the host's harness label, which the
orchestrator reads where its harness has no git of its own, as a replay's does.

What this is NOT
----------------
**It is not the CI job, and it is not a gate.** The CI job extracts its harness
from the target branch with ``git archive``, because in a merge request pipeline
the checkout is the MR and its copy of the harness is author-controlled. Here the
harness is the developer's own checkout, bind-mounted read-only, so a run tests
what is checked out — which is the point of running it locally before pushing, and
also means a local run says nothing about whether the pinned CI harness would
agree. Nothing is posted without ``--post``.

Properties it is built around
-----------------------------
1. **No credential value is ever an argv element.** AWS credentials are exported
   on the HOST with ``aws configure export-credentials``, because that is where
   the credential helper and SSO session live, and placed in the environment of
   the ``docker`` subprocess only. Containers receive them as ``-e NAME``, never
   ``-e NAME=value``, and so do ``UX_TEST_PERSONAS``, ``UX_TEST_UI_ENV``,
   ``UX_TEST_AWS_ACCOUNT_ID`` and, with ``--post`` only, ``GITLAB_REVIEW_TOKEN``.
   None of them is in an argv, so none shows in a process listing's command
   lines or in a shell history. They ARE visible to ``docker inspect``, as every
   container's environment is, which is a bound worth knowing rather than a
   leak: anyone who can run ``docker inspect`` on the host can already run a
   container as root on it. The ``browser`` container gets the personas only
   with ``--sign-in``, but it holds the exported AWS session and
   ``UX_TEST_UI_ENV`` for as long as it runs, and every process ``docker exec``
   starts in it inherits them, the recorder and its render included: in that mode
   the recorder is the developer's to start, not the harness's, so the CI
   orchestrator's minimal recorder environment does not apply.
2. **The MR's code runs only where there is nothing to take.** ``npm ci`` (with
   the MR's lifecycle scripts) and ``vite build`` (with the MR's config) run in a
   build container that receives no ``-e`` at all and sees the exported source
   read-only. The result reaches the review container only as static files,
   which the egress proxy serves at ``http://localhost:3000``.
3. **DevTools is published on loopback only.** The DevTools protocol is
   unauthenticated, so ``browser`` mode publishes the forwarder on
   ``127.0.0.1:<port>`` and never on every interface. Inside the container the
   forwarder listens on every interface, because that is where Docker delivers
   the published port, and relays only a connection from loopback or from the
   container's default gateway, the address that delivery comes from; another
   container on the same network, or a LAN peer routed to the bridge, is refused
   (``browser.py``, under "Who can reach the forwarder").
4. **It deletes nothing it did not create.** A non-empty output directory is
   refused unless this script created it (a marker file records that), because
   the build container empties ``<out>/build`` and the export replaces
   ``<out>/source``.
5. **A local run usually holds the host's whole session.** ``ai_ux_test.py``
   gives ``claude`` a self-assumed, Bedrock-only session when the credentials can
   re-assume their own role, and that rarely holds on a developer's host: an
   assumed-role ARN names its role without the role's path, so an SSO
   permission-set role (under ``/aws-reserved/sso.amazonaws.com/``, trusting only
   its SAML provider) cannot be re-assumed, and IAM-user keys have no role at all.
   Then ``claude`` runs on the exported session, often an administrator's, which a
   ``WARNING:`` line says before the session starts and ``summary.json`` records
   as ``credential_scope.claude: ambient``; and render, which is never handed
   those credentials, renders the video without narration.
6. **Standard library only, on any host Python >= 3.11.4.** It imports nothing
   from ``ux_runner``, needs neither make nor bash, and resolves ``docker``,
   ``git`` and ``aws`` from ``PATH``, so it runs as
   ``py scripts\\ux_runner\\run_in_docker.py`` on Windows. ``review`` also runs
   ``ai_ux_test.py --collect-only`` on the host, and ``stories`` runs
   ``ai_ux_test_stories.py --collect-only``, ``--merge`` and ``--judge``,
   ``stories.py list`` and ``changelog_select.py nudge`` there, so those
   scripts' imports have to hold there too. 3.11.4 is the first release whose
   ``tarfile`` has the ``filter='data'`` extraction the source export relies on.

Usage
-----
    # dry-run the UX test of MR 812 against idp-ux-nightly (the make target is
    # `make ai-ux-test-local MR=812`)
    python3 scripts/ux_runner/run_in_docker.py review --mr 812

    # the same, and post the section to the MR's review note (POST=1)
    python3 scripts/ux_runner/run_in_docker.py review --mr 812 --post

    # the confined browser alone, signed in as Admin; then stop it
    python3 scripts/ux_runner/run_in_docker.py browser --sign-in Admin
    python3 scripts/ux_runner/run_in_docker.py browser --stop

    # first-time-user stories (`make ai-ux-test-stories-local ...`)
    python3 scripts/ux_runner/run_in_docker.py stories --story find-where-to-start --as new-analyst
    python3 scripts/ux_runner/run_in_docker.py stories --all --jobs 3
    python3 scripts/ux_runner/run_in_docker.py stories --story find-where-to-start --interactive
    python3 scripts/ux_runner/run_in_docker.py stories --pair --story route-folder-to-profile --as platform-admin
    python3 scripts/ux_runner/run_in_docker.py stories --replay <a run's ux-test-stories/>

Unrecognised ``review`` options pass through to ``scripts/sdlc/ai_ux_test.py``,
for example ``--max-turns 40`` or ``--persona Admin``, and unrecognised options of
an unattended, watched or replayed ``stories`` run to
``scripts/sdlc/ai_ux_test_stories.py``, for example ``--docs <sha>``.

Environment
-----------
AWS credentials
    Whatever the host's ``aws`` CLI resolves: ``--profile``, ``AWS_PROFILE``, a
    credential helper or an SSO session. ``AWS_REGION`` (or
    ``AWS_DEFAULT_REGION``) names the stack's region.
``UX_TEST_PERSONAS``, ``UX_TEST_AWS_ACCOUNT_ID``
    As the CI variables hold them: base64 of the persona JSON, and the account's
    twelve digits. Required by ``review`` and by an unattended, watched or
    replayed ``stories`` run; ``browser`` needs the personas only for
    ``--sign-in``, and ``stories --pair`` needs only the personas.
``UX_TEST_UI_ENV``
    Optional. Without it, the stack's ``WebUITestEnvFile`` output and the names
    of the account's buckets that start with ``<stack>-`` are read on the host.
``GITLAB_REVIEW_TOKEN``, ``CI_API_V4_URL``, ``CI_PROJECT_ID``
    ``--post`` only.
``CI``, ``GITLAB_CI``
    Forwarded to the review container wherever the host sets them, so that
    ``ai_ux_test.py`` refuses ``--dev-video-any-stack`` on a CI host, and to every
    story container, the watched run's browser container included, so that a
    developer-only option the story orchestrator refuses under CI is refused there
    too.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import io
import json
import math
import os
import platform
import re
import shutil
import stat
import subprocess  # nosec B404 - fixed argv lists, never a shell
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any

IMAGE_NAME = "idp-ux-runner"
#: The image's build inputs, in the order :func:`image_tag` hashes them. Every one
#: except the Dockerfile is COPY'd into the image, which a test pins.
IMAGE_INPUTS = (
    "Dockerfile",
    "install.sh",
    "package.json",
    "package-lock.json",
    "requirements.txt",
    "chrome_policy.json",
)
CONTAINER_CDP_PORT = 9223

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parents[1]
DEFAULT_STACK = "idp-ux-nightly"
DEFAULT_TARGET_BRANCH = "develop"
DEFAULT_REMOTE = "origin"
DEFAULT_HOST_CDP_PORT = 9222
DEFAULT_BROWSER_NAME = "ux-browser"
NPM_CACHE_VOLUME = "idp-ux-runner-npm"
MINIMUM_PYTHON = (3, 11, 4)
OUT_MARKER = ".ux-runner-out"

#: What ``aws configure export-credentials --format env-no-export`` prints.
CREDENTIAL_NAMES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_CREDENTIAL_EXPIRATION",
)
#: Handed to the review and browser containers by name only (``-e NAME``); the
#: values travel in the environment of the ``docker`` subprocess. The last one
#: is not a secret: a container's stdout is a pipe, which Python block-buffers,
#: so without it the progress lines and browser.py's ready line would arrive only
#: when the process exits.
CONTAINER_ENV_NAMES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "UX_TEST_PERSONAS",
    "UX_TEST_UI_ENV",
    "UX_TEST_AWS_ACCOUNT_ID",
    "PYTHONUNBUFFERED",
)
#: Added to the review container with ``--post`` only, by name.
POST_ENV_NAMES = ("GITLAB_REVIEW_TOKEN", "CI_API_V4_URL", "CI_PROJECT_ID")
#: Added to the review container by name, so a host that is CI is CI inside it too
#: and ``ai_ux_test.py`` refuses ``--dev-video-any-stack`` there as it does in the CI
#: job. ``docker run -e NAME`` sets a variable only when the host has it set.
CI_ENV_NAMES = ("CI", "GITLAB_CI")
#: Added to the browser container, by name, so browser.py can print the host-side
#: DevTools URL and the ``docker exec`` prefix for the recorder.
BROWSER_ENV_NAMES = ("UX_BROWSER_HOST_PORT", "UX_BROWSER_CONTAINER")
#: Prefixes of host variables that never reach the docker CLI that runs the
#: build container, so a later ``-e`` there could not forward one by accident.
BUILD_ENV_DROPPED_PREFIXES = ("AWS_", "UX_TEST_", "GITLAB_", "CI_")

#: A tmpfs inside the container (Chrome's profile, the recorder's frames), not a
#: host path.
CONTAINER_TMPFS = "/tmp:rw,exec,mode=1777"  # nosec B108 - a container tmpfs spec
#: The forwarder listens on every interface INSIDE the container, where Docker
#: delivers the host port published on 127.0.0.1, and relays only a peer on
#: loopback or at the container's default gateway, the address that delivery
#: comes from; ``browser.peer_filter`` refuses every other address.
CONTAINER_CDP_LISTEN = f"0.0.0.0:{CONTAINER_CDP_PORT}"
#: The MR's UI is copied out of the read-only source mount and built on the
#: container's own filesystem: the host's export stays untouched (so a re-run can
#: replace it, even on Linux, where the container's files would be root-owned),
#: and node_modules never crosses a Docker Desktop file share.
UI_BUILD_COMMAND = (
    "cp -R /work/source/src/ui /tmp/ui && cd /tmp/ui"
    " && npm ci --no-audit --no-fund"
    " && node --max-old-space-size=4096 ./node_modules/.bin/vite build"
    " --outDir /work/build --emptyOutDir"
)

STACK_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
HEAD_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ACCOUNT_DIGITS_RE = re.compile(r"(?<![0-9])[0-9]{12}(?![0-9])")


class DockerRunError(Exception):
    """A host-side step failed or a precondition is unmet; the text says why."""


def image_tag(package_dir: Path) -> str:
    """The image tag for the toolchain the inputs in ``package_dir`` define.

    A content hash rather than a version, so an edit to any input (a new pin, a
    policy change) builds a new image instead of silently reusing a stale one.
    Each input is framed by its name and length, so bytes moving between two
    adjacent files still change the tag.
    """
    digest = hashlib.sha256()
    for name in IMAGE_INPUTS:
        path = package_dir / name
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise DockerRunError(
                f"{path} cannot be read ({exc.strerror}); the image is built from "
                f"{', '.join(IMAGE_INPUTS)}"
            ) from exc
        digest.update(f"{name}\0{len(data)}\0".encode())
        digest.update(data)
    return f"{IMAGE_NAME}:{digest.hexdigest()[:12]}"


def host_platform(machine: str | None = None) -> str:
    """The Docker platform matching the host's CPU, so nothing runs emulated.

    Windows reports ``AMD64`` or ``ARM64``, macOS ``arm64`` or ``x86_64`` and
    Linux ``aarch64`` or ``x86_64``.
    """
    name = (platform.machine() if machine is None else machine).lower()
    return "linux/arm64" if name in ("arm64", "aarch64") else "linux/amd64"


def bind(source: PurePath, target: str, *, readonly: bool) -> list[str]:
    """A ``--mount`` bind of ``source`` at ``target``, as two argv elements.

    ``--mount`` rather than ``-v`` because ``-v`` splits on ``:``, which a Windows
    path contains. ``--mount`` splits on ``,`` instead, so a source holding one
    is refused rather than allowed to inject a second mount option.
    """
    text = str(source)
    if "," in text:
        raise DockerRunError(
            f"{text} contains a comma, which a docker --mount source cannot carry; "
            "use a directory whose path has none"
        )
    spec = f"type=bind,source={text},target={target}"
    if readonly:
        spec += ",readonly"
    return ["--mount", spec]


def resolve_tool(name: str) -> str:
    """The absolute path of a host tool, so nothing depends on a shell's lookup."""
    found = shutil.which(name)
    if not found:
        raise DockerRunError(f"`{name}` is not on PATH; this step runs it on the host")
    return found


def _bounded(text: str, limit: int = 400) -> str:
    """One line of tool output for an error message: collapsed, short, no account."""
    flat = ACCOUNT_DIGITS_RE.sub("[account]", " ".join(text.split()))
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _env_flags(names: Sequence[str]) -> list[str]:
    """``-e NAME`` for each name: docker reads each value from its own environment."""
    return [part for name in names for part in ("-e", name)]


def export_credentials(
    *,
    profile: str | None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, str]:
    """The host's current AWS credentials, as the four ``env-no-export`` values.

    Exported on the host because that is where the credential helper or SSO
    session lives; a container could not refresh either. The result goes into a
    subprocess environment and nowhere else.
    """
    argv = [resolve_tool("aws"), "configure", "export-credentials"]
    argv += ["--format", "env-no-export"]
    if profile:
        argv += ["--profile", profile]
    completed = run(  # noqa: S603 - fixed argv, no shell
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise DockerRunError(
            f"`aws configure export-credentials` failed (exit {completed.returncode}): "
            f"{_bounded(completed.stderr or '')}. Refresh the host's AWS session "
            "(for example `aws sso login`) and retry."
        )
    values: dict[str, str] = {}
    for line in (completed.stdout or "").splitlines():
        name, separator, value = line.strip().partition("=")
        if separator and name in CREDENTIAL_NAMES and value:
            values[name] = value
    if "AWS_ACCESS_KEY_ID" not in values or "AWS_SECRET_ACCESS_KEY" not in values:
        raise DockerRunError(
            "`aws configure export-credentials` printed no access key; check that "
            "the host's AWS CLI is version 2 and is signed in"
        )
    return values


def ensure_image(
    tag: str,
    platform: str,
    package_dir: Path,
    *,
    docker: str,
    rebuild: bool,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> None:
    """Build the toolchain image unless an image with this content tag exists."""
    if not rebuild:
        inspected = run(  # noqa: S603 - fixed argv, no shell
            [docker, "image", "inspect", tag],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if inspected.returncode == 0:
            print(f"-> image {tag} is up to date", flush=True)
            return
    print(f"-> building image {tag} for {platform} (several minutes)", flush=True)
    built = run(  # noqa: S603 - fixed argv, no shell
        [docker, "build", "--platform", platform, "-t", tag]
        + ["-f", str(package_dir / "Dockerfile"), str(package_dir)],
        check=False,
    )
    if built.returncode != 0:
        raise DockerRunError(
            f"`docker build` of {tag} failed (exit {built.returncode}); its output "
            "is above"
        )


def prepare_out(path: Path) -> Path:
    """Create the output directory, refusing one this script did not create.

    The build container empties ``build/`` inside it and the export replaces
    ``source/``, so pointing ``--out`` at a directory of one's own would delete
    its contents. A marker file records that the directory is this script's.
    """
    out = path.expanduser().resolve()
    marker = out / OUT_MARKER
    if out.is_dir() and any(out.iterdir()) and not marker.is_file():
        raise DockerRunError(
            f"{out} is not empty and was not created by run_in_docker.py; "
            "pass a new or empty directory as --out"
        )
    out.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        "Written by scripts/ux_runner/run_in_docker.py; safe to delete.\n",
        encoding="utf-8",
    )
    return out


def read_head_sha(inputs: Path) -> str:
    """The MR head the host-side collection resolved, validated before git sees it."""
    try:
        data = json.loads(inputs.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DockerRunError(f"{inputs} is missing or not JSON ({exc})") from exc
    head = data.get("head_sha") if isinstance(data, dict) else None
    if not isinstance(head, str) or not HEAD_SHA_RE.match(head):
        raise DockerRunError(f"{inputs} carries no 40-hex head_sha")
    return head


def export_ui_source(
    repo: Path,
    head_sha: str,
    destination: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> Path:
    """``src/ui`` at ``head_sha``, extracted under ``destination``; its path.

    ``git archive`` rather than a checkout, so the developer's working tree and
    index are never touched. The archive is extracted with ``filter='data'``,
    which refuses absolute paths, parent traversal, links that leave the
    destination and device files, because the MR's tree is author-controlled.

    The archive is read into memory whole, with no bound, before it is extracted,
    so the MR decides how much memory this takes: as much as its ``src/ui`` tree
    holds uncompressed. That is the one child here whose output the MR sizes, and
    a bound would have to cut a tree the build needs whole; it runs on the
    developer's host, for an MR the developer chose to test. In CI nothing
    archives the MR's tree, since the build job builds from its own checkout, and
    the precheck reads git through a bound (``ui_change.git_prefix``).
    """
    if not HEAD_SHA_RE.match(head_sha):
        raise DockerRunError(f"{head_sha!r} is not a 40-hex commit")
    completed = run(  # noqa: S603 - fixed argv, no shell
        [resolve_tool("git"), "-C", str(repo), "archive", "--format=tar"]
        + [head_sha, "src/ui"],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        raise DockerRunError(
            f"`git archive {head_sha[:8]} src/ui` failed (exit "
            f"{completed.returncode}): {_bounded(stderr)}. Fetch the MR's branch "
            "first if the commit is not local."
        )
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r|") as archive:
            archive.extractall(destination, filter="data")  # nosec B202 - filter='data'
    except tarfile.TarError as exc:
        raise DockerRunError(
            f"the src/ui archive of {head_sha[:8]} was refused: {exc}"
        ) from exc
    ui = destination / "src" / "ui"
    if not (ui / "package.json").is_file():
        raise DockerRunError(f"{head_sha[:8]} has no src/ui/package.json to build")
    return ui


def _aws_text(
    arguments: Sequence[str],
    *,
    profile: str | None,
    region: str | None,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> str:
    """The text output of one read-only ``aws`` call on the host."""
    argv = [resolve_tool("aws"), *arguments]
    if region:
        argv += ["--region", region]
    if profile:
        argv += ["--profile", profile]
    completed = run(  # noqa: S603 - fixed argv, no shell
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise DockerRunError(
            f"`aws {' '.join(arguments[:2])}` failed (exit {completed.returncode}): "
            f"{_bounded(completed.stderr or '')}"
        )
    return (completed.stdout or "").strip()


def stack_output(
    stack: str,
    key: str,
    *,
    profile: str | None,
    region: str | None,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> str:
    """One output of the review stack, read on the host."""
    query = f"Stacks[0].Outputs[?OutputKey=='{key}'].OutputValue"
    value = _aws_text(
        ["cloudformation", "describe-stacks", "--stack-name", stack]
        + ["--query", query, "--output", "text"],
        profile=profile,
        region=region,
        run=run,
    )
    if not value or value == "None":
        raise DockerRunError(f"stack {stack} has no {key} output in this account")
    return value


def ui_env_text(
    environ: Mapping[str, str],
    *,
    stack: str,
    profile: str | None,
    region: str | None,
    run: Callable[..., subprocess.CompletedProcess[Any]],
) -> str:
    """The UI environment text (data_formats D-CIVARS), decoded.

    ``UX_TEST_UI_ENV`` when the host sets it, as CI does. Otherwise the stack's
    ``WebUITestEnvFile`` output, plus a ``UX_TEST_BUCKETS=`` line of the exact
    bucket names the caller's account owns under ``<stack>-``: the egress proxy
    takes exact bucket hosts only, never a name pattern, because bucket names are
    global and a pattern would reach other accounts' buckets.
    """
    encoded = environ.get("UX_TEST_UI_ENV", "").strip()
    if encoded:
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, ValueError) as exc:
            raise DockerRunError(
                "UX_TEST_UI_ENV is set but is not base64 of UTF-8 text"
            ) from exc
    text = stack_output(
        stack, "WebUITestEnvFile", profile=profile, region=region, run=run
    )
    listed = _aws_text(
        ["s3api", "list-buckets", "--query"]
        + [f"Buckets[?starts_with(Name, '{stack}-')].Name", "--output", "text"],
        profile=profile,
        region=region,
        run=run,
    )
    names = [name for name in listed.split() if name != "None"]
    lines = [text.rstrip("\n")]
    if names:
        lines.append(f"UX_TEST_BUCKETS={','.join(names)}")
    return "\n".join(lines) + "\n"


def _container_env(
    environ: Mapping[str, str], credentials: Mapping[str, str], ui_env: str
) -> dict[str, str]:
    """The ``docker`` subprocess environment that carries every ``-e NAME`` value.

    Inherited credential variables are dropped before the exported ones go in, so
    a stale session token beside fresh long-term keys cannot reach the container.
    """
    env = {
        name: value for name, value in environ.items() if name not in CREDENTIAL_NAMES
    }
    env.update(credentials)
    env["UX_TEST_UI_ENV"] = base64.b64encode(ui_env.encode("utf-8")).decode("ascii")
    env["PYTHONUNBUFFERED"] = "1"
    if not env.get("AWS_REGION") and env.get("AWS_DEFAULT_REGION"):
        env["AWS_REGION"] = env["AWS_DEFAULT_REGION"]
    return env


def _build_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The host environment minus everything a credential or token travels in."""
    return {
        name: value
        for name, value in environ.items()
        if not name.startswith(BUILD_ENV_DROPPED_PREFIXES)
    }


def _region(environ: Mapping[str, str]) -> str | None:
    return environ.get("AWS_REGION") or environ.get("AWS_DEFAULT_REGION") or None


def _check_stack(stack: str) -> None:
    if not STACK_NAME_RE.match(stack):
        raise DockerRunError(f"{stack!r} is not a CloudFormation stack name")


def _report_outputs(out: Path) -> None:
    artifacts = out / "ux-tests"
    section = artifacts / "section.md"
    if section.is_file():
        print(f"section.md: {section}")
    else:
        print(f"no section.md was written; see {artifacts / 'summary.json'}")
    for video in sorted(artifacts.glob("*.mp4")):
        print(f"video:      {video}")


def review(
    args: argparse.Namespace,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    environ: Mapping[str, str] | None = None,
) -> int:
    """The CI-equivalent UX test of one MR; returns the review container's exit code.

    Collection and the source export run on the host, where the git remote's SSH
    key is. The MR's UI is built in a container with no credentials. The test
    itself runs in a second container from the image, with the developer's
    checkout mounted read-only as its harness.
    """
    env = dict(os.environ if environ is None else environ)
    _check_stack(args.stack)
    if args.mr <= 0:
        raise DockerRunError("--mr must be a merge request iid, such as 812")
    for name in ("UX_TEST_PERSONAS", "UX_TEST_AWS_ACCOUNT_ID"):
        if not env.get(name):
            raise DockerRunError(
                f"{name} is not set on the host; export it as the CI variable "
                "holds it (see the Environment section of --help)"
            )
    if args.post and not env.get("GITLAB_REVIEW_TOKEN"):
        raise DockerRunError("--post needs GITLAB_REVIEW_TOKEN in the host environment")
    docker = resolve_tool(args.docker)
    default_out = REPO_ROOT / "scratch" / "ux-docker" / f"mr-{args.mr}"
    out = prepare_out(Path(args.out) if args.out else default_out)
    inputs = out / "inputs.json"

    print(f"-> collecting MR {args.mr} on the host", flush=True)
    collected = run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, str(REPO_ROOT / "scripts" / "sdlc" / "ai_ux_test.py")]
        + ["--mr", str(args.mr), "--collect-only", "--inputs", str(inputs)]
        + ["--repo-dir", str(REPO_ROOT), "--target-branch", args.target_branch]
        + ["--remote", args.remote],
        env=env,
        check=False,
    )
    if collected.returncode != 0:
        raise DockerRunError(
            f"collecting MR {args.mr} failed (exit {collected.returncode}); its "
            "output is above"
        )
    head_sha = read_head_sha(inputs)
    print(f"-> exporting src/ui at {head_sha[:8]}", flush=True)
    ui_dir = export_ui_source(REPO_ROOT, head_sha, out / "source", run=run)

    platform_name = host_platform()
    tag = image_tag(PACKAGE_DIR)
    ensure_image(
        tag,
        platform_name,
        PACKAGE_DIR,
        docker=docker,
        rebuild=args.rebuild_image,
        run=run,
    )
    credentials = export_credentials(profile=args.profile, run=run)
    ui_env = ui_env_text(
        env, stack=args.stack, profile=args.profile, region=_region(env), run=run
    )
    (ui_dir / ".env.production.local").write_text(ui_env, encoding="utf-8")

    build_dir = out / "build"
    build_dir.mkdir(exist_ok=True)
    print("-> building the MR's UI in a container with no credentials", flush=True)
    built = run(  # noqa: S603 - fixed argv, no shell
        [docker, "run", "--rm", "--init", "--platform", platform_name]
        + bind(out / "source", "/work/source", readonly=True)
        + bind(build_dir, "/work/build", readonly=False)
        + ["--mount", f"type=volume,source={NPM_CACHE_VOLUME},target=/root/.npm"]
        + [tag, "bash", "-c", UI_BUILD_COMMAND],
        env=_build_env(env),
        check=False,
    )
    if built.returncode != 0:
        raise DockerRunError(
            f"the MR's UI did not build (exit {built.returncode}); its output is above"
        )
    if not (build_dir / "index.html").is_file():
        raise DockerRunError(f"the UI build wrote no {build_dir / 'index.html'}")

    names = CONTAINER_ENV_NAMES + CI_ENV_NAMES + (POST_ENV_NAMES if args.post else ())
    if args.post:
        print(
            f"-> running the UX test; it posts to MR {args.mr} unless an option "
            "passed through to it makes the run a dry run",
            flush=True,
        )
    else:
        print("-> running the UX test as a dry run; nothing is posted", flush=True)
    completed = run(  # noqa: S603 - fixed argv, no shell
        [docker, "run", "--rm", "--init", "--platform", platform_name]
        + ["--shm-size", "1g", "--security-opt", "no-new-privileges"]
        + ["--tmpfs", CONTAINER_TMPFS]
        + bind(REPO_ROOT, "/repo", readonly=True)
        + bind(out, "/out", readonly=False)
        + _env_flags(names)
        + [tag, "python3", "/repo/scripts/sdlc/ai_ux_test.py", "--mr", str(args.mr)]
        + ["--inputs", "/out/inputs.json", "--build-dir", "/out/build"]
        + ["--artifact-dir", "/out/ux-tests", "--repo-dir", "/repo"]
        + ["--stack", args.stack]
        + ([] if args.post else ["--dry-run"])
        + list(getattr(args, "passthrough", [])),
        env=_container_env(env, credentials, ui_env),
        check=False,
    )
    _report_outputs(out)
    return completed.returncode


def audit_state(path: Path) -> str:
    """``state: <state>, <n> stray(s)`` of a ``browser-audit.json``, or why not."""
    try:
        audit = _read_json(path, AUDIT_BYTES, str(path))
    except DockerRunError as error:
        return f"state: unreadable, {error}"
    if not isinstance(audit, dict):
        return "state: unreadable, not a JSON object"
    state = audit.get("state")
    strays = audit.get("strays")
    shown = state if isinstance(state, str) and state.isidentifier() else "unknown"
    count = len(strays) if isinstance(strays, list) else 0
    return f"state: {shown}, {count} stray(s)"


def _is_ready_line(line: str) -> bool:
    """browser.py's one JSON line, which says the browser is ready to drive."""
    text = line.strip()
    if not text.startswith("{"):
        return False
    try:
        parsed = json.loads(text)
    except ValueError:
        return False
    return isinstance(parsed, dict) and "cdp" in parsed


def follow_logs(
    argv: Sequence[str],
    *,
    popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    echo: Callable[[str], None] = print,
) -> str | None:
    """Echo a log stream until browser.py's ready line; that line, or None.

    None means the stream ended first, which is the container exiting before it
    was ready. The follower is stopped either way: ``docker logs -f`` would
    otherwise outlive this script.
    """
    process = popen(  # noqa: S603 - fixed argv, no shell
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        for raw in process.stdout or ():
            line = raw.rstrip("\r\n")
            if _is_ready_line(line):
                return line
            echo(line)
        return None
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def browser(
    args: argparse.Namespace,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    environ: Mapping[str, str] | None = None,
    follow: Callable[[Sequence[str]], str | None] | None = None,
) -> int:
    """Start (or with --stop, stop) the confined browser; 0 once it is ready.

    The container is detached and removed when it stops. Its DevTools forwarder
    is published only on the host's 127.0.0.1, and the recorder runs inside it
    through ``docker exec``, because the recorder's Unix socket cannot cross a
    Docker Desktop VM and its render needs the image's ffmpeg.
    """
    env = dict(os.environ if environ is None else environ)
    if not CONTAINER_NAME_RE.match(args.name):
        raise DockerRunError(f"{args.name!r} is not a docker container name")
    docker = resolve_tool(args.docker)
    default_out = REPO_ROOT / "scratch" / "ux-docker" / "browser"
    requested_out = Path(args.out) if args.out else default_out
    if args.stop:
        print(f"-> stopping {args.name}", flush=True)
        stopped = run(  # noqa: S603 - fixed argv, no shell
            [docker, "stop", "-t", "30", args.name], check=False
        )
        audit = requested_out.expanduser().resolve() / "browser" / "browser-audit.json"
        if audit.is_file():
            print(f"socket audit: {audit} ({audit_state(audit)})")
        else:
            print(f"no socket audit at {audit}")
        return stopped.returncode
    _check_stack(args.stack)
    if not 0 < args.port < 65536:
        raise DockerRunError(f"--port {args.port} is not a TCP port")
    if args.sign_in and not env.get("UX_TEST_PERSONAS"):
        raise DockerRunError("--sign-in needs UX_TEST_PERSONAS in the host environment")
    build_dir = Path(args.build_dir).expanduser().resolve() if args.build_dir else None
    if build_dir is not None and not (build_dir / "index.html").is_file():
        raise DockerRunError(f"{build_dir} has no index.html; build the UI first")
    out = prepare_out(requested_out)

    platform_name = host_platform()
    tag = image_tag(PACKAGE_DIR)
    ensure_image(
        tag,
        platform_name,
        PACKAGE_DIR,
        docker=docker,
        rebuild=args.rebuild_image,
        run=run,
    )
    credentials = export_credentials(profile=args.profile, run=run)
    region = _region(env)
    ui_env = ui_env_text(
        env, stack=args.stack, profile=args.profile, region=region, run=run
    )
    web_url = args.web_url
    if not web_url and build_dir is None:
        web_url = stack_output(
            args.stack,
            "ApplicationWebURL",
            profile=args.profile,
            region=region,
            run=run,
        )
    child_env = _container_env(env, credentials, ui_env)
    child_env["UX_BROWSER_HOST_PORT"] = str(args.port)
    child_env["UX_BROWSER_CONTAINER"] = args.name

    names = tuple(
        name
        for name in CONTAINER_ENV_NAMES
        if name != "UX_TEST_PERSONAS" or args.sign_in
    )
    print(f"-> starting {args.name}; DevTools on 127.0.0.1:{args.port}", flush=True)
    started = run(  # noqa: S603 - fixed argv, no shell
        [docker, "run", "-d", "--rm", "--name", args.name, "--init"]
        + ["--platform", platform_name]
        + ["-p", f"127.0.0.1:{args.port}:{CONTAINER_CDP_PORT}"]
        + ["--shm-size", "1g", "--security-opt", "no-new-privileges"]
        + ["--tmpfs", CONTAINER_TMPFS]
        + bind(REPO_ROOT, "/repo", readonly=True)
        + bind(out, "/out", readonly=False)
        + (bind(build_dir, "/ui-build", readonly=True) if build_dir else [])
        + _env_flags(names + BROWSER_ENV_NAMES)
        + [tag, "python3", "/repo/scripts/ux_runner/browser.py", "--stack", args.stack]
        + (["--web-url", web_url] if web_url else [])
        + (["--build-dir", "/ui-build"] if build_dir else [])
        + (["--sign-in", args.sign_in] if args.sign_in else [])
        + ["--cdp-listen", CONTAINER_CDP_LISTEN, "--out", "/out/browser"],
        env=child_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if started.returncode != 0:
        raise DockerRunError(
            f"`docker run` of {args.name} failed (exit {started.returncode}): "
            f"{_bounded(started.stderr or '')}. If the name is taken, run "
            "`run_in_docker.py browser --stop` or pass --name."
        )
    ready = (follow or follow_logs)([docker, "logs", "-f", args.name])
    if ready is None:
        print(
            f"FAILED: {args.name} exited before it was ready; its output is above",
            file=sys.stderr,
        )
        return 1
    print(ready, flush=True)
    return 0


STORIES_ENTRY = "scripts/sdlc/ai_ux_test_stories.py"
STORY_PAIR_ENTRY = "scripts/ux_runner/story_pair.py"
STORY_LIST_ENTRY = "scripts/ux_runner/stories.py"
STORY_NUDGE_ENTRY = "scripts/ux_runner/changelog_select.py"
STORY_SEARCH_ENTRY = "scripts/ux_runner/docs_index.py"
STORY_CATALOGUE = REPO_ROOT / "scripts" / "ux_stories.toml"
STORIES_OUT = REPO_ROOT / "scratch" / "ux-docker" / "stories"
STORY_ARTIFACTS = "ux-test-stories"
STORY_SHARDS = "shards"
STORY_RUN_FILE = "story-run.json"
STORY_RESULTS_FILE = "results.json"
STORY_RUN_SCHEMA = "ux-test-stories/run"
STORY_INPUTS_SCHEMA = "ux-test-stories/inputs"
STORY_VERDICTS = (
    "not_refused",
    "stuck",
    "offered",
    "out_of_steps",
    "error",
    "gave_up",
    "unclear",
    "not_run",
    "refused_as_expected",
    "done",
)
BAD_STORY_VERDICTS = frozenset({"not_refused", "stuck", "offered"})
CONFIRMATION_LABELS = {
    "single": "",
    "unconfirmed": "unconfirmed (1 of 1)",
    "confirmed": "2 of 2",
    "mixed": "mixed (1 of 2)",
}
HARNESS_LINE_PREFIXES = ("FAILED:", "SKIPPED:", "WARNING:")
STORY_HARNESS_TREE = ("scripts", ".claude/skills", "docs-site/setup.sh")
STORY_GIT_CONFIG = (
    ("GIT_CONFIG_COUNT", "1"),
    ("GIT_CONFIG_KEY_0", "safe.directory"),
    ("GIT_CONFIG_VALUE_0", "*"),
)
STORY_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
FLOW_KEY_RE = re.compile(
    r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*@[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
)
VIDEO_POLICY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,19}$")
STORY_TOOL_RE = re.compile(r"^mcp__ux-story__[a-z][a-z_]{0,40}$")
OWNER_RE = re.compile(r"^[0-9]{1,10}:[0-9]{1,10}$")
POSIX_BARE_RE = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")
POWERSHELL_BARE_RE = re.compile(r"[A-Za-z0-9_./=-]+")
SEARCH_QUERY_CHARS = 200
MAX_STORY_JOBS = 16
MEMORY_PER_CONTAINER = 2 * 1024**3
WATCH_GRACE_SECONDS = 180
DEFAULT_FLOW_MINUTES = 12
PAIR_HOME = "/tmp/ux-pair"  # nosec B108 - a directory inside the browser container
PAIR_CONFIG = "pair-mcp.json"
PAIR_ENV_NAMES = ("UX_TEST_PERSONAS", "UX_TEST_UI_ENV", "PYTHONUNBUFFERED")
OPENING_MESSAGE = "Begin the task described in your instructions."
STORY_OPTIONS_SET_HERE = (
    "--artifact-dir",
    "--audit",
    "--check",
    "--collect-only",
    "--dev-unconfined",
    "--digest",
    "--inputs",
    "--judge",
    "--merge",
    "--precheck",
    "--repo-dir",
    "--shard",
    "--summary",
)
OWNER_PROBE = ".ux-owner-probe"
STORY_RECORD_BYTES = 16 * 1024 * 1024
STORY_INPUTS_BYTES = 32 * 1024 * 1024
PAIR_REPLY_BYTES = 256 * 1024
AUDIT_BYTES = 1024 * 1024


@dataclass(frozen=True)
class StoryFacts:
    """What the host needs of one catalogue story, read without the strict loader.

    ``personas`` pairs each persona of the story's ``as`` table with its Cognito
    group, in the catalogue's order. The orchestrator validates the catalogue in
    full inside the container; the host reads only what it acts on before then.
    """

    story: str
    personas: tuple[tuple[str, str], ...]
    first_visit: bool
    flow_minutes: int


def _catalogue_problem(catalogue: Path, detail: str) -> DockerRunError:
    return DockerRunError(
        f"{catalogue.name}: {detail}; `make ai-ux-test-stories-list` (Windows: "
        "`py scripts\\ux_runner\\stories.py list`) says what the catalogue holds"
    )


def story_facts(story: str, catalogue: Path | None = None) -> StoryFacts:
    """The personas, groups, ``first_visit`` and ``flow_minutes`` of ``story``.

    ``tomllib`` is imported here rather than at the top, so a Python older than
    :data:`MINIMUM_PYTHON` still reaches :func:`main`'s version message.
    """
    import tomllib

    path = STORY_CATALOGUE if catalogue is None else catalogue
    if not STORY_ID_RE.match(story):
        raise DockerRunError(f"{story!r} is not a story id (lower-case words and -)")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise _catalogue_problem(path, f"cannot be read ({exc})") from exc
    personas = data.get("personas")
    entries = data.get("stories")
    if not isinstance(personas, dict) or not isinstance(entries, list):
        raise _catalogue_problem(path, "has no [personas] table or no [[stories]]")
    entry = next(
        (e for e in entries if isinstance(e, dict) and e.get("id") == story), None
    )
    if entry is None:
        raise _catalogue_problem(path, f"has no story {story!r}")
    table = entry.get("as")
    if not isinstance(table, dict) or not table:
        raise _catalogue_problem(path, f"story {story!r} has no `as` table")
    pairs: list[tuple[str, str]] = []
    for name in table:
        record = personas.get(name)
        group = record.get("group") if isinstance(record, dict) else None
        if not isinstance(group, str) or not STORY_ID_RE.match(str(name)):
            raise _catalogue_problem(
                path, f"story {story!r} runs as {name!r}, which has no group"
            )
        pairs.append((str(name), group))
    limits = data.get("limits", {})
    minutes = (
        limits.get("flow_minutes", DEFAULT_FLOW_MINUTES)
        if isinstance(limits, dict)
        else DEFAULT_FLOW_MINUTES
    )
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 1:
        raise _catalogue_problem(path, "[limits] flow_minutes is not a whole number")
    return StoryFacts(
        story=story,
        personas=tuple(pairs),
        first_visit=entry.get("first_visit") is True,
        flow_minutes=minutes,
    )


def choose_persona(facts: StoryFacts, persona: str | None) -> tuple[str, str]:
    """The persona a watched or paired flow runs as, and its group.

    A story with one persona needs no ``--as``; one with several names them.
    """
    names = [name for name, _ in facts.personas]
    if persona is None:
        if len(facts.personas) == 1:
            return facts.personas[0]
        raise DockerRunError(
            f"{facts.story} runs as {', '.join(names)}; choose one with --as"
        )
    for name, group in facts.personas:
        if name == persona:
            return name, group
    raise DockerRunError(
        f"{facts.story} does not run as {persona!r}; its personas are "
        f"{', '.join(names)}"
    )


def story_mode(args: argparse.Namespace) -> str:
    """Which stories mode the options ask for, refusing a combination that clashes.

    One of ``list``, ``search``, ``pair``, ``watch``, ``replay`` and ``run`` (the
    unattended default), with every option the chosen mode does not read refused
    by name rather than ignored.
    """
    modes = (
        ("list", "--list", bool(args.list)),
        ("search", "--search", args.search is not None),
        ("pair", "--pair", bool(args.pair)),
        ("watch", "--interactive", bool(args.interactive)),
        ("replay", "--replay", args.replay is not None),
    )
    chosen = [(name, flag) for name, flag, on in modes if on]
    if len(chosen) > 1:
        raise DockerRunError(
            f"{chosen[0][1]} and {chosen[1][1]} are different modes; choose one"
        )
    mode, mode_flag = chosen[0] if chosen else ("run", "an unattended run")
    selection = [
        flag
        for flag, on in (
            ("--story", args.story is not None),
            ("--mr", args.mr is not None),
            ("--all", bool(args.all)),
        )
        if on
    ]
    if args.story is not None and not STORY_ID_RE.match(args.story):
        raise DockerRunError(f"--story {args.story!r} is not a story id")
    if args.persona is not None and not STORY_ID_RE.match(args.persona):
        raise DockerRunError(f"--as {args.persona!r} is not a persona id")
    if args.persona is not None and args.story is None:
        raise DockerRunError(
            "--as needs --story: it names one of that story's personas"
        )
    if args.mr is not None and args.mr <= 0:
        raise DockerRunError("--mr must be a merge request iid, such as 812")
    if not 1 <= args.jobs <= MAX_STORY_JOBS:
        raise DockerRunError(f"--jobs is a container count from 1 to {MAX_STORY_JOBS}")
    if args.video is not None and not VIDEO_POLICY_RE.match(args.video):
        raise DockerRunError(f"--video {args.video!r} is not a video policy word")
    if args.port is not None and not 0 < args.port < 65536:
        raise DockerRunError(f"--port {args.port} is not a TCP port")
    reads = {
        "list": set(),
        "search": set(),
        "pair": {"--story", "--port", "--name", "--build-dir"}
        | {"--pair-full", "--pair-keychain"},
        "watch": {"--story", "--port", "--name", "--build-dir", "passthrough"},
        "replay": {"--video", "passthrough"},
        "run": {"--story", "--mr", "--all", "--jobs", "--video", "--post"}
        | {"--build-dir", "passthrough"},
    }[mode]
    given = {
        "--story": args.story is not None,
        "--mr": args.mr is not None,
        "--all": bool(args.all),
        "--jobs": args.jobs != 1,
        "--video": args.video is not None,
        "--post": bool(args.post),
        "--port": args.port is not None,
        "--name": args.name != DEFAULT_BROWSER_NAME,
        "--build-dir": args.build_dir is not None,
        "--pair-full": bool(args.pair_full),
        "--pair-keychain": bool(args.pair_keychain),
        "passthrough": bool(args.passthrough),
    }
    unread = [flag for flag, on in given.items() if on and flag not in reads]
    if "passthrough" in unread:
        raise DockerRunError(
            f"unrecognized arguments for {mode_flag}: {' '.join(args.passthrough)}"
        )
    if unread:
        raise DockerRunError(f"{unread[0]} does not apply to {mode_flag}")
    _check_passthrough(args.passthrough)
    if mode == "watch":
        named = sorted(
            {token.split("=", 1)[0] for token in args.passthrough}
            & {"--run-usd", "--reruns"}
        )
        if named:
            raise DockerRunError(
                f"{named[0]} does not apply to --interactive: a watched flow is "
                "bounded by its step budget and wall clock, and is never re-run"
            )
    if mode in ("pair", "watch") and selection != ["--story"]:
        raise DockerRunError(f"{mode_flag} needs --story, and --as when it has several")
    if mode == "run":
        if len(selection) != 1:
            raise DockerRunError(
                "an unattended run takes one of --story (with or without --as), "
                "--mr or --all"
            )
        if args.jobs > 1 and args.mr is not None:
            raise DockerRunError("--jobs shards --story and --all runs, never an MR's")
        if args.post and args.mr is None:
            raise DockerRunError("--post posts an MR's run, so it needs --mr")
        if args.build_dir is not None and args.mr is not None:
            raise DockerRunError("--mr builds the MR's own UI; --build-dir is not read")
    if args.pair_full and args.pair_keychain:
        raise DockerRunError("--pair-full already drops --bare; choose one")
    return mode


def _check_passthrough(passthrough: Sequence[str]) -> None:
    """Refuse an orchestrator option this script sets or must never pass in.

    The orchestrator reads the last of a repeated option, so a run directory,
    checkout, inputs file or shard passed through would replace this script's
    and write outside the run's ``ux-test-stories/``, and a mode of the
    orchestrator's own (:data:`STORY_OPTIONS_SET_HERE`) would run something other
    than the mode chosen here. ``--dev-unconfined`` drops the managed policy and
    the uid switch, which in a story container are the confinement, so it is
    never passed in, whether ``CI`` is set or not.
    """
    named = {token.split("=", 1)[0] for token in passthrough if token.startswith("--")}
    if "--dev-unconfined" in named:
        raise DockerRunError(
            "--dev-unconfined is for a development host's own Chrome; a story "
            "container is the confinement, so it is never passed in"
        )
    refused = sorted(named & set(STORY_OPTIONS_SET_HERE))
    if refused:
        raise DockerRunError(
            f"{refused[0]} is not passed through: this script sets the run's "
            "directory, checkout and mode itself"
        )


def _read_json(path: Path, limit: int, what: str) -> Any:
    """A JSON file of at most ``limit`` bytes, opened without following a link."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DockerRunError(
            f"{what} cannot be read ({exc.strerror or type(exc).__name__})"
        ) from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise DockerRunError(f"{what} is not a regular file")
    with os.fdopen(descriptor, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise DockerRunError(f"{what} is larger than {limit} bytes")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise DockerRunError(f"{what} is not JSON") from exc


def story_run_dir(
    label: str, *, out: str | None, clock: Callable[[], float] = time.time
) -> Path:
    """A new ``scratch/ux-docker/stories/<UTC time>-<label>`` directory, or ``--out``.

    The name sorts by time, and a second run in the same second takes the next
    free suffix, so a run never writes into another's directory.
    """
    if out:
        return prepare_out(Path(out))
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock()))
    for number in range(1, 100):
        suffix = "" if number == 1 else f"-{number}"
        candidate = STORIES_OUT / f"{stamp}-{label}{suffix}"
        if not candidate.exists():
            return prepare_out(candidate)
    raise DockerRunError(f"{STORIES_OUT} already holds 99 runs started at {stamp}")


def _git_text(repo: Path, arguments: Sequence[str], *, run: Callable[..., Any]) -> str:
    completed = run(  # noqa: S603 - fixed argv, no shell
        [resolve_tool("git"), "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise DockerRunError(
            f"`git {' '.join(arguments[:2])}` failed (exit {completed.returncode}): "
            f"{_bounded(completed.stderr or '')}"
        )
    return completed.stdout or ""


def harness_facts(repo: Path, *, run: Callable[..., Any]) -> tuple[str, bool]:
    """The checkout's harness label and whether its ``docs/`` differ from HEAD.

    The label is ``<sha>``, ``<sha>+dirty`` when the harness paths hold changes
    (untracked files included) or ``unknown`` when git cannot say; the docs count
    as changed when git cannot say either, which is the safe answer for a record.
    """
    try:
        head = _git_text(repo, ["rev-parse", "HEAD"], run=run).strip()
        harness = _git_text(
            repo, ["status", "--porcelain", "--", *STORY_HARNESS_TREE], run=run
        )
        docs = _git_text(repo, ["status", "--porcelain", "--", "docs"], run=run)
    except DockerRunError:
        return "unknown", True
    if not HEAD_SHA_RE.match(head):
        return "unknown", True
    return (f"{head}+dirty" if harness.strip() else head), bool(docs.strip())


def checkout_git_mounts(repo: Path, *, run: Callable[..., Any]) -> list[str]:
    """The shared git directory of a worktree checkout, mounted where its ``.git``
    file names it, read-only.

    A worktree's ``.git`` is a file naming a directory of the main checkout by its
    host path, which a container does not have, so git inside the container would
    find no repository at ``/repo``. Mounting that directory at the same path
    makes the orchestrator's docs-from-a-commit work there. Windows paths cannot
    be mount points in a Linux container, so a Windows worktree gets none.
    """
    if os.name == "nt" or not (repo / ".git").is_file():
        return []
    try:
        common = _git_text(repo, ["rev-parse", "--git-common-dir"], run=run).strip()
    except DockerRunError:
        return []
    if not common:
        return []
    shared = (repo / common).resolve()
    if not shared.is_dir() or shared == repo or repo in shared.parents:
        return []
    return bind(shared, shared.as_posix(), readonly=True)


def git_variables(harness: str) -> dict[str, str]:
    """What git and the orchestrator read in a story container, by name.

    ``safe.directory`` covers root reading a checkout its developer owns, which
    git otherwise refuses on a Linux host, as command-line configuration through
    ``GIT_CONFIG_COUNT``; ``UX_HARNESS_SHA`` is the harness label the
    orchestrator falls back on where its harness has no git of its own.
    """
    variables = dict(STORY_GIT_CONFIG)
    if harness != "unknown":
        variables["UX_HARNESS_SHA"] = harness
    return variables


def read_story_inputs(path: Path) -> tuple[str, bool]:
    """The MR head the host collected, and whether the MR changes ``src/ui``.

    Reads the story job's ``inputs.json`` (``ux-test-stories/inputs`` v1, whose
    ``mr`` member is the MR UX test's record), or that record on its own.
    """
    data = _read_json(path, STORY_INPUTS_BYTES, str(path))
    if not isinstance(data, dict):
        raise DockerRunError(f"{path} is not a JSON object")
    record = data.get("mr") if data.get("schema") == STORY_INPUTS_SCHEMA else data
    if not isinstance(record, dict):
        raise DockerRunError(f"{path} carries no MR record")
    head = record.get("head_sha")
    if not isinstance(head, str) or not HEAD_SHA_RE.match(head):
        raise DockerRunError(f"{path} carries no 40-hex head_sha")
    changed = record.get("changed")
    if not isinstance(changed, list):
        raise DockerRunError(f"{path} carries no list of changed files")
    paths = [
        entry.get(key)
        for entry in changed
        if isinstance(entry, dict)
        for key in ("path", "old_path")
    ]
    ui = any(
        isinstance(value, str) and (value == "src/ui" or value.startswith("src/ui/"))
        for value in paths
    )
    return head, ui


def docker_memory(docker: str, *, run: Callable[..., Any]) -> int | None:
    """The memory Docker gives its containers, in bytes, or None when unread."""
    completed = run(  # noqa: S603 - fixed argv, no shell
        [docker, "info", "--format", "{{json .MemTotal}}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    text = (completed.stdout or "").strip()
    if completed.returncode != 0 or not text.isdigit():
        return None
    return int(text)


def _memory_lines(memory: int | None, jobs: int) -> list[str]:
    need = jobs * MEMORY_PER_CONTAINER
    if memory is None:
        return [
            f"WARNING: Docker's memory limit could not be read; {jobs} containers "
            f"need about {need / 1024**3:.0f} GiB"
        ]
    lines = [
        f"-> Docker has {memory / 1024**3:.1f} GiB of memory; {jobs} containers "
        f"need about {need / 1024**3:.0f} GiB"
    ]
    if need > memory:
        lines.append(
            f"WARNING: --jobs {jobs} is more than Docker's memory holds; too little "
            "memory shows as timeouts, not as an error (Docker Desktop: Settings > "
            "Resources)"
        )
    return lines


def _foreign_entries(root: Path, uid: int) -> bool:
    """Whether anything under ``root`` is owned by another user, or cannot be read."""
    unreadable: list[OSError] = []
    for directory, names, files in os.walk(root, onerror=unreadable.append):
        for name in [*names, *files]:
            try:
                if os.lstat(os.path.join(directory, name)).st_uid != uid:
                    return True
            except OSError:
                return True
        try:
            if os.lstat(directory).st_uid != uid:
                return True
        except OSError:
            return True
    return bool(unreadable)


def return_ownership(
    out: Path,
    *,
    docker: str,
    tag: str,
    platform_name: str,
    run: Callable[..., Any],
    echo: Callable[[str], None] = print,
) -> None:
    """Give the run's files back to the developer where containers left them root's.

    A container writes as root, and on a Linux host whose Docker is not rootless
    those files stay root's on the host, so the run could not be re-judged there.
    Where anything under ``out`` belongs to another user, a container with no
    network and no credentials hands it back (``chown -hR``, which follows no
    link). Docker Desktop and rootless Docker already map ownership, so on those
    nothing runs. Windows has no uid to hand back.
    """
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is None or getgid is None:
        return
    uid = int(getuid())
    if not _foreign_entries(out, uid):
        return
    echo("-> handing the run's files back to you; the containers wrote them as root")
    run(  # noqa: S603 - fixed argv, no shell
        [docker, "run", "--rm", "--network", "none", "--platform", platform_name]
        + bind(out, "/out", readonly=False)
        + [tag, "chown", "-hR", f"{uid}:{int(getgid())}", "/out"],
        check=False,
    )
    if _foreign_entries(out, uid):
        echo(
            f"WARNING: some files under {out} still belong to another user, so "
            "re-judging it on this host may fail"
        )


def owner_probe(
    docker: str,
    name: str,
    out: Path,
    *,
    run: Callable[..., Any],
) -> str | None:
    """``uid:gid`` for ``docker exec --user`` when the container's root is foreign.

    The browser container writes a probe into ``out``; when it lands owned by
    another user, the pair session's server runs as the developer instead, so the
    journal it writes (mode 0600) can be judged on the host. ``None`` on Windows,
    on Docker Desktop and under rootless Docker, where root's files are already
    the developer's.
    """
    getuid = getattr(os, "getuid", None)
    getgid = getattr(os, "getgid", None)
    if getuid is None or getgid is None:
        return None
    probe = out / OWNER_PROBE
    run(  # noqa: S603 - fixed argv, no shell
        [docker, "exec", name, "touch", f"/out/{OWNER_PROBE}"],
        check=False,
        capture_output=True,
    )
    try:
        owner = os.lstat(probe).st_uid
    except OSError:
        return None
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    uid = int(getuid())
    return None if owner == uid else f"{uid}:{int(getgid())}"


def shard_containers(out: Path, jobs: int) -> list[str]:
    """One docker container name per shard of the run in ``out``, unique to the run."""
    run_name = re.sub(r"[^A-Za-z0-9_.-]", "-", out.name)[:40].strip("-.") or "run"
    return [f"ux-stories-{run_name}-{index}" for index in range(1, jobs + 1)]


def _tagged(line: str, tag: str) -> str:
    """A shard's output line, with the harness's own keywords kept at column 0."""
    for prefix in HARNESS_LINE_PREFIXES:
        if line.startswith(prefix):
            return f"{prefix} {tag}:{line[len(prefix) :]}"
    return f"[{tag}] {line}"


def run_shards(
    commands: Sequence[tuple[str, Sequence[str]]],
    *,
    env: Mapping[str, str],
    popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    echo: Callable[[str], None] = print,
    containers: Sequence[str] = (),
    kill: Callable[[str], None] | None = None,
) -> list[int]:
    """Run each ``(tag, argv)`` at once; their exit codes, in order.

    Each container's output is relayed line by line with its tag, and a
    ``FAILED:``, ``SKIPPED:`` or ``WARNING:`` line keeps that word first, so the
    harness's own lines can still be found by it. An interrupt stops every
    container: ``docker run`` passes the signal on to the container it runs. A
    ``docker run`` still running a minute later is killed, and so, through
    ``kill``, is the container named at its place in ``containers``, which
    killing the client alone would leave running.
    """
    lock = threading.Lock()
    started: list[subprocess.Popen[str]] = []
    relays: list[threading.Thread] = []

    def relay(stream: Any, tag: str) -> None:
        for raw in stream:
            with lock:
                echo(_tagged(raw.rstrip("\r\n"), tag))

    try:
        for tag, argv in commands:
            process = popen(  # noqa: S603 - fixed argv, no shell
                list(argv),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=dict(env),
            )
            started.append(process)
            thread = threading.Thread(
                target=relay, args=(process.stdout, tag), daemon=True
            )
            thread.start()
            relays.append(thread)
        codes = [process.wait() for process in started]
    except BaseException:
        for process in started:
            if process.poll() is None:
                process.terminate()
        for position, process in enumerate(started):
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                if kill is not None and position < len(containers):
                    kill(containers[position])
                process.kill()
        raise
    for thread in relays:
        thread.join(timeout=10)
    return codes


def _flow_attempts(
    results: Mapping[str, Any],
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    """``results.json``'s attempts by flow key, and how many entries were refused.

    Only the structured fields the per-flow line prints are read, each against its
    closed set or as a bounded integer; the free text beside them is never touched.
    """
    flows = results.get("flows")
    grouped: dict[str, list[dict[str, Any]]] = {}
    refused = 0
    for entry in flows if isinstance(flows, list) else ():
        if not isinstance(entry, dict):
            refused += 1
            continue
        key = entry.get("key")
        attempt = entry.get("attempt")
        if (
            not isinstance(key, str)
            or not FLOW_KEY_RE.match(key)
            or isinstance(attempt, bool)
            or not isinstance(attempt, int)
            or entry.get("verdict") not in STORY_VERDICTS
        ):
            refused += 1
            continue
        grouped.setdefault(key, []).append(entry)
    return grouped, refused


def _count(value: object, limit: int = 10**6) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= limit:
        return None
    return value


def flow_line(key: str, attempts: Sequence[Mapping[str, Any]]) -> str:
    """The flow's line, in the orchestrator's shape: verdict; steps; hesitations.

    The verdict and its state follow the judge's confirmation rule: one attempt is
    single, or unconfirmed when bad; a second attempt at twice the budget decides
    on its own; two at the same budget are 2 of 2 when they agree and mixed when
    they do not, showing the first. Only ``results.json``'s closed-set words and
    whole numbers are printed, never its text.
    """
    ordered = sorted(attempts, key=lambda entry: int(entry["attempt"]))
    shown = ordered[0]
    if len(ordered) == 1:
        state = "unconfirmed" if shown["verdict"] in BAD_STORY_VERDICTS else "single"
    elif ordered[1].get("budget_scale") == 2:
        shown = ordered[1]
        state = "unconfirmed" if shown["verdict"] in BAD_STORY_VERDICTS else "single"
    elif ordered[1]["verdict"] == shown["verdict"]:
        state = "confirmed"
    else:
        state = "mixed"
    used = _count(shown.get("actions_used"))
    budget = _count(shown.get("budget"))
    steps = (
        f"{used} of {budget} steps"
        if used is not None and budget is not None
        else "steps not counted"
    )
    notes = shown.get("notes")
    confusion = notes.get("confusion") if isinstance(notes, dict) else None
    hesitations = len(confusion) if isinstance(confusion, list) else None
    said = (
        "hesitations not read"
        if hesitations is None
        else f"{hesitations} hesitation{'' if hesitations == 1 else 's'}"
    )
    label = CONFIRMATION_LABELS[state]
    verdict = f"{shown['verdict']} ({label})" if label else str(shown["verdict"])
    return f"flow {key}: {verdict}; {steps}; {said}"


def _dollars(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    amount = float(value)
    return amount if math.isfinite(amount) and 0 <= amount < 10**6 else None


def cost_line(summary: object) -> str | None:
    """``results.json``'s summary of what the run cost, as the orchestrator's final
    line words it, or None when the summary does not hold the figures."""
    if not isinstance(summary, Mapping):
        return None
    counted = _dollars(summary.get("counted_usd"))
    pinned = _dollars(summary.get("pinned_usd"))
    without = _count(summary.get("sessions_without_usage"))
    if counted is None or pinned is None or without is None:
        return None
    text = f"${counted:.2f} counted, ${pinned:.2f} at the pinned rates"
    if without:
        text += f" ({without} session{'' if without == 1 else 's'} reported no usage)"
    return f"cost:       {text}"


def report_story_run(artifacts: Path, *, echo: Callable[[str], None] = print) -> None:
    """Name the run's section and videos, then one line per flow, then the cost."""
    section = artifacts / "section.md"
    if section.is_file():
        echo(f"section.md: {section}")
    if artifacts.is_dir():
        for video in sorted(artifacts.rglob("*.mp4")):
            echo(f"video:      {video}")
    results = artifacts / STORY_RESULTS_FILE
    if not results.is_file():
        echo(f"no {STORY_RESULTS_FILE} was written; see {artifacts}")
        return
    try:
        data = _read_json(results, STORY_RECORD_BYTES, str(results))
    except DockerRunError as exc:
        echo(f"WARNING: {exc}")
        return
    grouped, refused = _flow_attempts(data if isinstance(data, dict) else {})
    echo(f"results:    {results}")
    if refused:
        echo(
            f"WARNING: {refused} entr{'y' if refused == 1 else 'ies'} of {results} "
            "are not the v1 shape and are not shown"
        )
    for key in sorted(grouped):
        echo(flow_line(key, grouped[key]))
    cost = cost_line(data.get("summary") if isinstance(data, dict) else None)
    if cost is not None:
        echo(cost)


def _story_names(*, post: bool) -> tuple[str, ...]:
    """What a container a model runs in receives by name: the review container's
    names, CI's two, and the review token's names only to post."""
    return CONTAINER_ENV_NAMES + CI_ENV_NAMES + (POST_ENV_NAMES if post else ())


def _orchestrator(
    selection: Sequence[str],
    *,
    artifact_dir: str,
    stack: str,
    web_url: str | None,
    build_dir: str | None,
    extra: Sequence[str] = (),
    isolated: bool = False,
    repo_dir: str = "/repo",
) -> list[str]:
    """The orchestrator's command line inside a container, pass-through last.

    ``isolated`` adds ``-I``, for the run ``docker exec`` starts in the browser
    container, whose environment is the container's own.
    """
    flags = ["-I", "-B"] if isolated else ["-B"]
    command = ["python3", *flags, f"/repo/{STORIES_ENTRY}", *selection]
    command += [
        "--artifact-dir",
        artifact_dir,
        "--repo-dir",
        repo_dir,
        "--stack",
        stack,
    ]
    if web_url:
        command += ["--web-url", web_url]
    if build_dir:
        command += ["--build-dir", build_dir]
    return command + list(extra)


def _review_container(
    *,
    docker: str,
    platform_name: str,
    tag: str,
    harness: Path,
    out: Path,
    names: Sequence[str],
    command: Sequence[str],
    mounts: Sequence[str] = (),
    name: str | None = None,
) -> list[str]:
    """``docker run`` of one story container: the MR UX test's review container.

    ``name`` names the container, which a shard's is so that it can be stopped
    when its ``docker run`` cannot.
    """
    return (
        [docker, "run", "--rm", "--init", "--platform", platform_name]
        + (["--name", name] if name else [])
        + ["--shm-size", "1g", "--security-opt", "no-new-privileges"]
        + ["--tmpfs", CONTAINER_TMPFS]
        + bind(harness, "/repo", readonly=True)
        + bind(out, "/out", readonly=False)
        + list(mounts)
        + _env_flags(names)
        + [tag, *command]
    )


def _require(env: Mapping[str, str], *names: str) -> None:
    for name in names:
        if not env.get(name):
            raise DockerRunError(
                f"{name} is not set on the host; export it as the CI variable holds "
                "it (see the Environment section of --help)"
            )


def _ui_env_web_url(ui_env: str) -> str | None:
    """The ``UX_TEST_WEB_URL=`` line CI's ``UX_TEST_UI_ENV`` carries, when present."""
    for line in ui_env.splitlines():
        name, separator, value = line.strip().partition("=")
        if separator and name == "UX_TEST_WEB_URL" and value.strip():
            return value.strip()
    return None


def _web_url(
    args: argparse.Namespace,
    ui_env: str,
    stack: str,
    *,
    region: str | None,
    run: Callable[..., Any],
) -> str:
    """``--web-url``, else the UI environment's line, else the stack's output."""
    return (
        args.web_url
        or _ui_env_web_url(ui_env)
        or stack_output(
            stack, "ApplicationWebURL", profile=args.profile, region=region, run=run
        )
    )


def _build_mr_ui(
    *,
    docker: str,
    platform_name: str,
    tag: str,
    out: Path,
    head: str,
    ui_env: str,
    env: Mapping[str, str],
    run: Callable[..., Any],
) -> Path:
    """The MR's ``src/ui`` exported on the host and built where nothing is to take."""
    print(f"-> exporting src/ui at {head[:8]}", flush=True)
    ui_dir = export_ui_source(REPO_ROOT, head, out / "source", run=run)
    (ui_dir / ".env.production.local").write_text(ui_env, encoding="utf-8")
    build_dir = out / "build"
    build_dir.mkdir(exist_ok=True)
    print("-> building the MR's UI in a container with no credentials", flush=True)
    built = run(  # noqa: S603 - fixed argv, no shell
        [docker, "run", "--rm", "--init", "--platform", platform_name]
        + bind(out / "source", "/work/source", readonly=True)
        + bind(build_dir, "/work/build", readonly=False)
        + ["--mount", f"type=volume,source={NPM_CACHE_VOLUME},target=/root/.npm"]
        + [tag, "bash", "-c", UI_BUILD_COMMAND],
        env=_build_env(env),
        check=False,
    )
    if built.returncode != 0:
        raise DockerRunError(
            f"the MR's UI did not build (exit {built.returncode}); its output is above"
        )
    if not (build_dir / "index.html").is_file():
        raise DockerRunError(f"the UI build wrote no {build_dir / 'index.html'}")
    return build_dir


def _local_build(args: argparse.Namespace) -> Path | None:
    if args.build_dir is None:
        return None
    build = Path(args.build_dir).expanduser().resolve()
    if not (build / "index.html").is_file():
        raise DockerRunError(f"{build} has no index.html; build the UI first")
    return build


def _stories_run(
    args: argparse.Namespace,
    *,
    run: Callable[..., Any],
    popen: Callable[..., subprocess.Popen[str]],
    environ: Mapping[str, str],
) -> int:
    """An unattended run: one container, or ``--jobs`` shards merged afterwards."""
    env = dict(environ)
    stack = args.stack or DEFAULT_STACK
    _check_stack(stack)
    _require(env, "UX_TEST_PERSONAS", "UX_TEST_AWS_ACCOUNT_ID")
    if args.post:
        if stack != DEFAULT_STACK:
            raise DockerRunError(
                f"--post posts only a run against the review stack {DEFAULT_STACK}"
            )
        _require(env, *POST_ENV_NAMES)
    story = str(args.story or "")
    if story:
        facts = story_facts(story)
        if args.persona is not None:
            choose_persona(facts, args.persona)
    local_build = _local_build(args)
    docker = resolve_tool(args.docker)
    if args.mr is not None:
        label = f"mr{args.mr}"
    elif args.all:
        label = "all"
    else:
        label = story + (f"@{args.persona}" if args.persona else "")
    out = story_run_dir(label, out=args.out)
    print(f"-> run directory: {out}", flush=True)
    selection: list[str]
    mr_ui = False
    head = ""
    if args.mr is not None:
        inputs = out / "inputs.json"
        print(f"-> collecting MR {args.mr} on the host", flush=True)
        collected = run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-B", str(REPO_ROOT / STORIES_ENTRY)]
            + ["--mr", str(args.mr), "--collect-only", "--inputs", str(inputs)]
            + ["--repo-dir", str(REPO_ROOT), "--target-branch", args.target_branch]
            + ["--remote", args.remote],
            env=env,
            check=False,
        )
        if collected.returncode != 0:
            raise DockerRunError(
                f"collecting MR {args.mr} failed (exit {collected.returncode}); its "
                "output is above"
            )
        head, mr_ui = read_story_inputs(inputs)
        selection = ["--mr", str(args.mr), "--inputs", "/out/inputs.json"]
    elif args.all:
        selection = ["--all"]
    else:
        selection = ["--story", story]
        if args.persona:
            selection += ["--as", str(args.persona)]
    platform_name = host_platform()
    tag = image_tag(PACKAGE_DIR)
    ensure_image(
        tag,
        platform_name,
        PACKAGE_DIR,
        docker=docker,
        rebuild=args.rebuild_image,
        run=run,
    )
    if args.jobs > 1:
        for line in _memory_lines(docker_memory(docker, run=run), args.jobs):
            print(line, flush=True)
    credentials = export_credentials(profile=args.profile, run=run)
    region = _region(env)
    ui_env = ui_env_text(env, stack=stack, profile=args.profile, region=region, run=run)
    web_url = (
        None
        if local_build is not None
        else _web_url(args, ui_env, stack, region=region, run=run)
    )
    mounts: list[str] = []
    build_dir: str | None = None
    if mr_ui:
        _build_mr_ui(
            docker=docker,
            platform_name=platform_name,
            tag=tag,
            out=out,
            head=head,
            ui_env=ui_env,
            env=env,
            run=run,
        )
        build_dir = "/out/build"
    elif local_build is not None:
        mounts = bind(local_build, "/ui-build", readonly=True)
        build_dir = "/ui-build"
    extra = (["--video", args.video] if args.video else []) + (
        ["--post"] if args.post else []
    )
    extra += list(args.passthrough)
    harness, _ = harness_facts(REPO_ROOT, run=run)
    variables = git_variables(harness)
    mounts += checkout_git_mounts(REPO_ROOT, run=run)
    names = _story_names(post=args.post) + tuple(variables)
    child_env = _container_env(env, credentials, ui_env)
    child_env.update(variables)
    if args.post:
        print(
            f"-> running the stories; they post to MR {args.mr} unless an option "
            "passed through makes the run a dry run",
            flush=True,
        )
    else:
        print("-> running the stories as a dry run; nothing is posted", flush=True)
    if args.jobs == 1:
        completed = run(  # noqa: S603 - fixed argv, no shell
            _review_container(
                docker=docker,
                platform_name=platform_name,
                tag=tag,
                harness=REPO_ROOT,
                out=out,
                names=names,
                mounts=mounts,
                command=_orchestrator(
                    selection,
                    artifact_dir=f"/out/{STORY_ARTIFACTS}",
                    stack=stack,
                    web_url=web_url,
                    build_dir=build_dir,
                    extra=extra,
                ),
            ),
            env=child_env,
            check=False,
        )
        codes = [completed.returncode]
    else:
        (out / STORY_SHARDS).mkdir(exist_ok=True)
        containers = shard_containers(out, args.jobs)
        commands = [
            (
                f"shard {index}/{args.jobs}",
                _review_container(
                    docker=docker,
                    platform_name=platform_name,
                    tag=tag,
                    harness=REPO_ROOT,
                    out=out,
                    names=names,
                    mounts=mounts,
                    name=containers[index - 1],
                    command=_orchestrator(
                        [*selection, "--shard", f"{index}/{args.jobs}"],
                        artifact_dir=f"/out/{STORY_SHARDS}/shard-{index}",
                        stack=stack,
                        web_url=web_url,
                        build_dir=build_dir,
                        extra=extra,
                    ),
                ),
            )
            for index in range(1, args.jobs + 1)
        ]
        codes = run_shards(
            commands,
            env=child_env,
            popen=popen,
            containers=containers,
            kill=lambda container: run(  # noqa: S603 - fixed argv, no shell
                [docker, "kill", container], check=False, capture_output=True
            ),
        )
    return_ownership(out, docker=docker, tag=tag, platform_name=platform_name, run=run)
    if args.jobs > 1:
        print(f"-> merging {args.jobs} shards", flush=True)
        shards = [
            str(out / STORY_SHARDS / f"shard-{index}")
            for index in range(1, args.jobs + 1)
        ]
        merged = run(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-B", str(REPO_ROOT / STORIES_ENTRY), "--merge"]
            + [*shards, "--out", str(out / STORY_ARTIFACTS)],
            env=env,
            check=False,
        )
        codes.append(merged.returncode)
    report_story_run(out / STORY_ARTIFACTS)
    return next((code for code in codes if code != 0), 0)


def _story_browser(
    *,
    docker: str,
    name: str,
    platform_name: str,
    port: int,
    tag: str,
    out: Path,
    audit_out: tuple[Path, str],
    stack: str,
    web_url: str | None,
    build_dir: Path | None,
    group: str,
    keep_welcome: bool,
    names: Sequence[str],
    mounts: Sequence[str] = (),
) -> list[str]:
    """``docker run -d`` of the browser container a watched or paired flow uses.

    Browser mode's container, signed in as the persona's group, keeping the welcome
    page for a ``first_visit`` story, with the run directory at ``/out``.
    ``audit_out`` is where ``browser-audit.json`` lands: a host directory and its
    mount point inside the container.
    """
    audit_host, audit_target = audit_out
    binds = bind(REPO_ROOT, "/repo", readonly=True) + bind(out, "/out", readonly=False)
    if audit_target != "/out":
        binds += bind(audit_host, audit_target, readonly=False)
    if build_dir is not None:
        binds += bind(build_dir, "/ui-build", readonly=True)
    return (
        [docker, "run", "-d", "--rm", "--name", name, "--init"]
        + ["--platform", platform_name]
        + ["-p", f"127.0.0.1:{port}:{CONTAINER_CDP_PORT}"]
        + ["--shm-size", "1g", "--security-opt", "no-new-privileges"]
        + ["--tmpfs", CONTAINER_TMPFS]
        + binds
        + list(mounts)
        + _env_flags(names)
        + [tag, "python3", "/repo/scripts/ux_runner/browser.py", "--stack", stack]
        + (["--web-url", web_url] if web_url else [])
        + (["--build-dir", "/ui-build"] if build_dir is not None else [])
        + ["--sign-in", group]
        + (["--keep-welcome"] if keep_welcome else [])
        + ["--cdp-listen", CONTAINER_CDP_LISTEN, "--out", f"{audit_target}/browser"]
    )


def _start_browser(
    argv: Sequence[str],
    *,
    name: str,
    docker: str,
    env: Mapping[str, str],
    run: Callable[..., Any],
    follow: Callable[[Sequence[str]], str | None],
) -> None:
    started = run(  # noqa: S603 - fixed argv, no shell
        list(argv),
        env=dict(env),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if started.returncode != 0:
        raise DockerRunError(
            f"`docker run` of {name} failed (exit {started.returncode}): "
            f"{_bounded(started.stderr or '')}. If the name is taken, run "
            "`run_in_docker.py browser --stop` or pass --name."
        )
    try:
        ready = follow([docker, "logs", "-f", name])
    except BaseException:
        _stop_container(docker, name, run=run)
        raise
    if ready is None:
        _stop_container(docker, name, run=run)
        raise DockerRunError(f"{name} exited before it was ready; its output is above")


def _stop_container(docker: str, name: str, *, run: Callable[..., Any]) -> None:
    """``docker stop`` with browser mode's 30 seconds, so browser.py writes its audit."""
    print(f"-> stopping {name}; it writes the socket audit", flush=True)
    run(  # noqa: S603 - fixed argv, no shell
        [docker, "stop", "-t", "30", name], check=False, capture_output=True
    )


def _browser_env(
    env: Mapping[str, str],
    credentials: Mapping[str, str],
    ui_env: str,
    *,
    port: int,
    name: str,
) -> dict[str, str]:
    child = _container_env(env, credentials, ui_env)
    child["UX_BROWSER_HOST_PORT"] = str(port)
    child["UX_BROWSER_CONTAINER"] = name
    return child


def _judge_on_host(
    artifacts: Path,
    audit: Path,
    env: Mapping[str, str],
    ui_env: str,
    *,
    run: Callable[..., Any],
) -> int:
    """``--judge --audit`` on the host, with the UI environment for the redactor.

    The orchestrator ran inside the browser container and exited before the
    container's stop wrote ``browser-audit.json``, so the judge folds that audit
    into the run before it judges, as an unattended run's own audit is.
    """
    judged = dict(env)
    judged.setdefault(
        "UX_TEST_UI_ENV", base64.b64encode(ui_env.encode("utf-8")).decode("ascii")
    )
    completed = run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-B", str(REPO_ROOT / STORIES_ENTRY), "--judge"]
        + [str(artifacts), "--audit", str(audit)],
        env=judged,
        check=False,
    )
    return completed.returncode


def _stories_watch(
    args: argparse.Namespace,
    *,
    run: Callable[..., Any],
    popen: Callable[..., subprocess.Popen[str]],
    environ: Mapping[str, str],
    follow: Callable[[Sequence[str]], str | None],
    terminal: Callable[[], bool],
) -> int:
    """Watch a confined flow live; this host starts and stops its browser container.

    The pinned Claude TUI runs in the browser container through ``docker exec -it``.
    When it exits, or ``flow_minutes`` and :data:`WATCH_GRACE_SECONDS` pass, the
    container is stopped, which writes the socket audit, the audit is folded into
    ``story-run.json`` and the run is judged on the host.
    """
    env = dict(environ)
    stack = args.stack or DEFAULT_STACK
    _check_stack(stack)
    _require(env, "UX_TEST_PERSONAS", "UX_TEST_AWS_ACCOUNT_ID")
    if not CONTAINER_NAME_RE.match(args.name):
        raise DockerRunError(f"{args.name!r} is not a docker container name")
    if not terminal():
        raise DockerRunError(
            "--interactive runs a terminal session, so run it from your own terminal "
            "(under mintty, put winpty in front of it); a tool without a TTY cannot"
        )
    facts = story_facts(args.story)
    persona, group = choose_persona(facts, args.persona)
    local_build = _local_build(args)
    port = args.port or DEFAULT_HOST_CDP_PORT
    docker = resolve_tool(args.docker)
    out = story_run_dir(f"watch-{facts.story}@{persona}", out=args.out)
    print(f"-> run directory: {out}", flush=True)
    platform_name = host_platform()
    tag = image_tag(PACKAGE_DIR)
    ensure_image(
        tag,
        platform_name,
        PACKAGE_DIR,
        docker=docker,
        rebuild=args.rebuild_image,
        run=run,
    )
    credentials = export_credentials(profile=args.profile, run=run)
    region = _region(env)
    ui_env = ui_env_text(env, stack=stack, profile=args.profile, region=region, run=run)
    web_url = (
        None if local_build else _web_url(args, ui_env, stack, region=region, run=run)
    )
    harness, _ = harness_facts(REPO_ROOT, run=run)
    variables = git_variables(harness)
    names = _story_names(post=False) + tuple(variables) + BROWSER_ENV_NAMES
    browser_env = _browser_env(env, credentials, ui_env, port=port, name=args.name)
    browser_env.update(variables)
    print(f"-> starting {args.name}; DevTools on 127.0.0.1:{port}", flush=True)
    _start_browser(
        _story_browser(
            docker=docker,
            name=args.name,
            platform_name=platform_name,
            port=port,
            tag=tag,
            out=out,
            audit_out=(out, "/out"),
            stack=stack,
            web_url=web_url,
            build_dir=local_build,
            group=group,
            keep_welcome=facts.first_visit,
            names=names,
            mounts=checkout_git_mounts(REPO_ROOT, run=run),
        ),
        name=args.name,
        docker=docker,
        env=browser_env,
        run=run,
        follow=follow,
    )
    deadline = facts.flow_minutes * 60 + WATCH_GRACE_SECONDS
    exit_code = 1
    stopped = False
    try:
        print(
            f"-> to watch, open chrome://inspect, Configure, add localhost:{port}, "
            "then inspect the page",
            flush=True,
        )
        print(
            "-> the session's system prompt holds the brief and the task: once it "
            "shows its prompt, give it a few seconds to start its ux-story server, "
            f"then type: {OPENING_MESSAGE}",
            flush=True,
        )
        command = _orchestrator(
            ["--interactive", "--story", facts.story, "--as", persona],
            artifact_dir=f"/out/{STORY_ARTIFACTS}",
            stack=stack,
            web_url=web_url,
            build_dir="/ui-build" if local_build else None,
            extra=args.passthrough,
            isolated=True,
        )
        process = popen(  # noqa: S603 - fixed argv, no shell
            [docker, "exec", "-it", "-e", "TERM", args.name, *command],
            env=_build_env(env),
        )
        try:
            exit_code = process.wait(timeout=deadline)
        except subprocess.TimeoutExpired:
            print(
                f"WARNING: {facts.flow_minutes} minutes (flow_minutes) and "
                f"{WATCH_GRACE_SECONDS} s more have passed; stopping {args.name}",
                flush=True,
            )
            _stop_container(docker, args.name, run=run)
            stopped = True
            try:
                exit_code = process.wait(timeout=90)
            except subprocess.TimeoutExpired:
                process.kill()
                exit_code = process.wait()
    finally:
        if not stopped:
            _stop_container(docker, args.name, run=run)
    return_ownership(out, docker=docker, tag=tag, platform_name=platform_name, run=run)
    artifacts = out / STORY_ARTIFACTS
    if not (artifacts / STORY_RUN_FILE).is_file():
        print(
            f"FAILED: the session wrote no {STORY_RUN_FILE} (exit {exit_code}); its "
            "output is above",
            flush=True,
        )
        return exit_code or 1
    judged = _judge_on_host(
        artifacts, out / "browser" / "browser-audit.json", env, ui_env, run=run
    )
    report_story_run(artifacts)
    return exit_code or judged


def _pair_reply(text: str, out: Path) -> dict[str, Any]:
    """The pair preparer's last output line, checked before any of it is used.

    The server's command must be ``/usr/bin/env -i`` with an empty environment
    map and arguments that are plain strings, every tool must be a ``ux-story``
    tool name, and the prompt must be a file under ``/out``, so what the host
    writes and prints is the shape it expects whatever the line says.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    try:
        reply = json.loads(lines[-1]) if lines else None
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        raise DockerRunError(
            "the pair preparer printed no JSON line; its output is above"
        )
    server = reply.get("server")
    tools = reply.get("allowed_tools")
    prompt = reply.get("prompt")
    if (
        not isinstance(server, dict)
        or server.get("command") != "/usr/bin/env"
        or server.get("env") != {}
        or not isinstance(server.get("args"), list)
        or not server["args"]
        or server["args"][0] != "-i"
        or not all(isinstance(part, str) for part in server["args"])
    ):
        raise DockerRunError("the pair preparer's server is not an `env -i` command")
    if (
        not isinstance(tools, list)
        or not tools
        or not all(isinstance(t, str) and STORY_TOOL_RE.match(t) for t in tools)
    ):
        raise DockerRunError("the pair preparer named a tool that is not a story tool")
    if not isinstance(prompt, str) or not prompt.startswith("/out/") or ".." in prompt:
        raise DockerRunError("the pair preparer's prompt is not under /out")
    host_prompt = out.joinpath(*PurePath(prompt[len("/out/") :]).parts)
    if not host_prompt.is_file():
        raise DockerRunError(f"the pair preparer wrote no {host_prompt}")
    return {"server": server, "tools": list(tools), "prompt": host_prompt}


def write_pair_config(
    path: Path,
    *,
    docker: str,
    name: str,
    owner: str | None,
    server: Mapping[str, Any],
) -> Path:
    """``pair-mcp.json``: one server, ``ux-story``, through ``docker exec -i``.

    The file is created afresh with mode 0600 and never through a link. The
    server's own command is the preparer's ``env -i`` line, so inside the
    container it inherits none of the AWS session, ``UX_TEST_UI_ENV`` or personas
    the container's environment holds, and ``--user`` runs it as the developer
    where :func:`owner_probe` found the container's root to be foreign.
    """
    if owner is not None and not OWNER_RE.match(owner):
        raise DockerRunError(f"{owner!r} is not a uid:gid")
    args = ["exec", "-i"] + (["--user", owner] if owner else [])
    args += [name, str(server["command"]), *server["args"]]
    payload = {
        "mcpServers": {
            "ux-story": {"type": "stdio", "command": docker, "args": args, "env": {}}
        }
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        if hasattr(os, "fchmod"):
            os.fchmod(handle.fileno(), 0o600)
        handle.write((json.dumps(payload, indent=2) + "\n").encode("utf-8"))
    return path


def _posix_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'" if value else "''"


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def pair_command(
    *,
    config: Path,
    prompt: Path,
    tools: Sequence[str],
    cwd: Path,
    full: bool,
    keychain: bool,
    shell: str,
) -> str:
    """The command that starts the pair session, for ``posix`` or ``powershell``.

    It changes into ``cwd`` first and starts ``claude`` only when that worked. The
    confined form is ``--bare --strict-mcp-config --tools=`` (``=`` keeps the tool
    list empty on every shell, Windows PowerShell 5.1's dropped ``''`` included),
    the story tools granted, ``--permission-mode manual``, and the brief and the
    task in ``--append-system-prompt-file``. ``keychain`` drops ``--bare``;
    ``full`` drops the confinement as well.
    """
    quote = _powershell_quote if shell == "powershell" else _posix_quote
    argv = ["claude"]
    if not (full or keychain):
        argv.append("--bare")
    if not full:
        argv += ["--strict-mcp-config"]
    argv += ["--mcp-config", str(config)]
    if not full:
        argv.append("--tools=")
    argv += ["--allowedTools", *tools, "--permission-mode", "manual"]
    argv += ["--append-system-prompt-file", str(prompt)]
    bare = POWERSHELL_BARE_RE if shell == "powershell" else POSIX_BARE_RE
    words = [part if bare.fullmatch(part) else quote(part) for part in argv]
    if shell == "powershell":
        return f"Set-Location -LiteralPath {quote(str(cwd))} && {' '.join(words)}"
    return f"cd {quote(str(cwd))} && {' '.join(words)}"


def pair_directory(scratch: Callable[[], Path] | None = None) -> Path:
    """A fresh empty directory outside the checkout for the pair session to start in.

    Claude Code reads ``CLAUDE.md`` from its working directory and every parent,
    and keys its auto-memory by that directory, so a session started inside the
    checkout would know this repository's own notes about the product.
    """
    made = (scratch or (lambda: Path(tempfile.mkdtemp(prefix="ux-pair-"))))()
    resolved = made.resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        raise DockerRunError(
            f"{resolved} is inside the checkout, where the session would read its "
            "CLAUDE.md; set TMPDIR to a directory outside it"
        )
    return resolved


def pair_after_commands(
    *, out: Path, name: str, python: str, shell: str
) -> tuple[str, str]:
    """The two commands a pair session ends with, quoted for ``posix`` or ``powershell``.

    Both run with this script's own interpreter, which passed its version check.
    The first stops the browser container named ``name``, which writes the socket
    audit into the run directory and prints its state; the second judges the run
    with that audit folded in, as a watched run is judged.
    """
    quote = _powershell_quote if shell == "powershell" else _posix_quote
    call = "& " if shell == "powershell" else ""
    script = REPO_ROOT / "scripts" / "ux_runner" / "run_in_docker.py"
    stop = (
        f"{call}{quote(python)} {quote(str(script))} browser --stop "
        f"--name {quote(name)} --out {quote(str(out))}"
    )
    judge = (
        f"{call}{quote(python)} -B {quote(str(REPO_ROOT / STORIES_ENTRY))} "
        f"--judge {quote(str(out / STORY_ARTIFACTS))} "
        f"--audit {quote(str(out / 'browser' / 'browser-audit.json'))} --summary"
    )
    return stop, judge


def _pair_lines(
    *,
    out: Path,
    config: Path,
    prompt: Path,
    tools: Sequence[str],
    cwd: Path,
    full: bool,
    keychain: bool,
    key: str,
    group: str,
    stack: str,
    windows: bool,
    name: str = DEFAULT_BROWSER_NAME,
    python: str = sys.executable,
) -> list[str]:
    """What the pair target prints: the command, the opening message, then after."""
    lines = [
        f"-> {key} is ready: signed in as {group} on {stack}.",
        "Start the session in your own terminal "
        + ("(PowerShell 7):" if windows else "(bash or zsh):"),
        "",
    ]
    shells = ("powershell", "posix") if windows else ("posix",)
    for shell in shells:
        if shell == "posix" and windows:
            lines += ["or in Git Bash:", ""]
        lines.append(
            "  "
            + pair_command(
                config=config,
                prompt=prompt,
                tools=tools,
                cwd=cwd,
                full=full,
                keychain=keychain,
                shell=shell,
            )
        )
        lines.append("")
    lines += [
        "Give it a few seconds to start its ux-story server, then send it this "
        "opening message (sent sooner, it reaches the model before the story tools):",
        "",
        f"  {OPENING_MESSAGE}",
        "",
    ]
    if full:
        lines.append(
            "WARNING: --pair-full starts your whole session: your CLAUDE.md, memory, "
            "settings, built-in tools and MCP servers, beside the story tools. Use it "
            "only for trusted UI and docs, with permission prompts on."
        )
    elif keychain:
        lines.append(
            f"The session starts in {cwd}, outside the checkout, so this repository's "
            "CLAUDE.md and auto-memory do not reach it; without --bare your user-level "
            "CLAUDE.md, hooks and plugins do."
        )
    else:
        lines.append(
            f"The session starts in {cwd}, outside the checkout, with --bare, so no "
            "CLAUDE.md, auto-memory, hook or plugin reaches it. --bare reads no "
            "keychain: it signs in through Bedrock or an API key, from your "
            "environment or your settings.json's env. For a keychain sign-in, use "
            "PAIR=keychain (--pair-keychain)."
        )
    lines += [
        f"The brief and the task reach it in {prompt}.",
        "",
        "When the flow has ended, stop the session, then stop the browser, which "
        "writes its socket audit, and judge the run with that audit:",
    ]
    for shell in shells:
        if windows:
            lines.append("in PowerShell:" if shell == "powershell" else "in Git Bash:")
        lines += [
            f"  {command}"
            for command in pair_after_commands(
                out=out, name=name, python=python, shell=shell
            )
        ]
    return lines


def _stories_pair(
    args: argparse.Namespace,
    *,
    run: Callable[..., Any],
    environ: Mapping[str, str],
    follow: Callable[[Sequence[str]], str | None],
    scratch: Callable[[], Path] | None,
) -> int:
    """Start the browser for a pair session and print the command that opens it.

    The container is left running for the session. The browser mode's ``--stop``,
    given the run directory, stops it and finds its socket audit there, and the
    printed judge folds that audit into the run. ``--pair-full`` is refused on
    the shared review stack.
    """
    env = dict(environ)
    stack = args.stack or DEFAULT_STACK
    _check_stack(stack)
    if args.pair_full and stack == DEFAULT_STACK:
        raise DockerRunError(
            f"--pair-full gives the session your shell, your credentials and this "
            f"checkout, and {DEFAULT_STACK} holds data any merge request's UI build "
            "can write, so the full session pairs only on a stack of your own "
            "(--stack); the confined session pairs on any"
        )
    _require(env, "UX_TEST_PERSONAS")
    if not CONTAINER_NAME_RE.match(args.name):
        raise DockerRunError(f"{args.name!r} is not a docker container name")
    facts = story_facts(args.story)
    persona, group = choose_persona(facts, args.persona)
    local_build = _local_build(args)
    port = args.port or DEFAULT_HOST_CDP_PORT
    docker = resolve_tool(args.docker)
    out = story_run_dir(f"pair-{facts.story}@{persona}", out=args.out)
    print(f"-> run directory: {out}", flush=True)
    platform_name = host_platform()
    tag = image_tag(PACKAGE_DIR)
    ensure_image(
        tag,
        platform_name,
        PACKAGE_DIR,
        docker=docker,
        rebuild=args.rebuild_image,
        run=run,
    )
    region = _region(env)
    ui_env = ui_env_text(env, stack=stack, profile=args.profile, region=region, run=run)
    web_url = (
        None if local_build else _web_url(args, ui_env, stack, region=region, run=run)
    )
    names = PAIR_ENV_NAMES + BROWSER_ENV_NAMES
    browser_env = _browser_env(env, {}, ui_env, port=port, name=args.name)
    print(f"-> starting {args.name}; DevTools on 127.0.0.1:{port}", flush=True)
    _start_browser(
        _story_browser(
            docker=docker,
            name=args.name,
            platform_name=platform_name,
            port=port,
            tag=tag,
            out=out,
            audit_out=(out, "/out"),
            stack=stack,
            web_url=web_url,
            build_dir=local_build,
            group=group,
            keep_welcome=facts.first_visit,
            names=names,
        ),
        name=args.name,
        docker=docker,
        env=browser_env,
        run=run,
        follow=follow,
    )
    try:
        owner = owner_probe(docker, args.name, out, run=run)
        harness, docs_dirty = harness_facts(REPO_ROOT, run=run)
        prepared = run(  # noqa: S603 - fixed argv, no shell
            [docker, "exec"]
            + (["--user", owner] if owner else [])
            + [args.name, "python3", "-I", "-B", f"/repo/{STORY_PAIR_ENTRY}"]
            + ["prepare", "--story", facts.story, "--as", persona, "--run", "/out"]
            + ["--stack", stack, "--harness", harness]
            + ["--docs-dirty", "1" if docs_dirty else "0", "--home", PAIR_HOME]
            + (["--build"] if local_build else []),
            env=_build_env(env),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if prepared.returncode != 0:
            raise DockerRunError(
                f"preparing the pair session failed (exit {prepared.returncode}): "
                f"{_bounded((prepared.stdout or '') + ' ' + (prepared.stderr or ''))}"
            )
        reply = _pair_reply((prepared.stdout or "")[-PAIR_REPLY_BYTES:], out)
        config = write_pair_config(
            out / PAIR_CONFIG,
            docker=docker,
            name=args.name,
            owner=owner,
            server=reply["server"],
        )
        cwd = REPO_ROOT if args.pair_full else pair_directory(scratch)
    except BaseException:
        _stop_container(docker, args.name, run=run)
        raise
    for line in _pair_lines(
        out=out,
        config=config,
        prompt=reply["prompt"],
        tools=reply["tools"],
        cwd=cwd,
        full=bool(args.pair_full),
        keychain=bool(args.pair_keychain),
        key=f"{facts.story}@{persona}",
        group=group,
        stack=stack,
        windows=os.name == "nt",
        name=args.name,
    ):
        print(line, flush=True)
    return 0


def export_tree(
    repo: Path,
    ref: str,
    pathspecs: Sequence[str],
    destination: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> Path:
    """``pathspecs`` at ``ref``, extracted into ``destination``, as for ``src/ui``.

    ``git archive`` rather than a checkout, and extracted with ``filter='data'``.
    """
    if not HEAD_SHA_RE.match(ref):
        raise DockerRunError(f"{ref!r} is not a 40-hex commit")
    completed = run(  # noqa: S603 - fixed argv, no shell
        [resolve_tool("git"), "-C", str(repo), "archive", "--format=tar", ref]
        + ["--", *pathspecs],
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        raise DockerRunError(
            f"`git archive {ref[:8]}` failed (exit {completed.returncode}): "
            f"{_bounded(stderr)}"
        )
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r|") as archive:
            archive.extractall(destination, filter="data")  # nosec B202 - filter='data'
    except tarfile.TarError as exc:
        raise DockerRunError(f"the archive of {ref[:8]} was refused: {exc}") from exc
    return destination


def replayable_harness(
    repo: Path,
    sha: str,
    *,
    remote: str,
    target: str,
    run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
) -> str:
    """The ref that holds ``sha``: ``<remote>/<target>`` or this checkout's ``HEAD``.

    A replay builds and runs the harness at ``sha`` with the host's credentials and
    every persona's password, and ``sha`` comes from a ``story-run.json``, which a
    merge request's own pipeline can write naming any commit. So it runs only a
    commit already merged to the target branch or already in the checkout this
    script runs from; anything else is refused before it is extracted.
    """
    for ref in (f"refs/remotes/{remote}/{target}", "HEAD"):
        completed = run(  # noqa: S603 - fixed argv, no shell
            [resolve_tool("git"), "-C", str(repo), "merge-base", "--is-ancestor"]
            + [sha, ref],
            capture_output=True,
            check=False,
        )
        if completed.returncode == 0:
            return ref
    raise DockerRunError(
        f"the run names harness commit {sha[:8]}, which is on neither "
        f"{remote}/{target} nor this checkout's history, so it is not run with "
        f"your credentials. A run's story-run.json can name any commit; if this "
        f"one is merged, `git fetch {remote} {target}` and replay again"
    )


def replay_plan(record: Mapping[str, Any]) -> dict[str, Any]:
    """What the host needs to replay a run's ``story-run.json``, each field checked.

    The harness commit (required), whether it was dirty, the stack, and the UI
    source: a build at a commit, made again here, or the deployed UI. The flows,
    model, effort, budgets and docs commit are the orchestrator's to read.
    """
    if record.get("schema") != STORY_RUN_SCHEMA or record.get("version") != 1:
        raise DockerRunError(f"the run's {STORY_RUN_FILE} is not {STORY_RUN_SCHEMA} v1")
    if record.get("mode") == "pair":
        raise DockerRunError(
            "a pair run was played by a session you started, so it has no model or "
            "effort to replay; run its story unattended instead"
        )
    harness = record.get("harness_sha")
    if not isinstance(harness, str) or not HEAD_SHA_RE.match(harness):
        raise DockerRunError(
            f"the run's {STORY_RUN_FILE} names no 40-hex harness_sha to replay"
        )
    stack = record.get("stack")
    name = stack.get("name") if isinstance(stack, dict) else None
    source = record.get("ui_source")
    kind = source.get("kind") if isinstance(source, dict) else None
    head = source.get("head_sha") if isinstance(source, dict) else None
    if kind == "build" and not (isinstance(head, str) and HEAD_SHA_RE.match(head)):
        raise DockerRunError(
            "the run's UI was a build, but it names no 40-hex head_sha"
        )
    if kind not in ("build", "deployed"):
        raise DockerRunError(f"the run's ui_source kind {kind!r} cannot be replayed")
    return {
        "harness": harness,
        "dirty": record.get("harness_dirty") is True,
        "stack": name if isinstance(name, str) and STACK_NAME_RE.match(name) else None,
        "build_head": head if kind == "build" else None,
    }


def _stories_replay(
    args: argparse.Namespace,
    *,
    run: Callable[..., Any],
    environ: Mapping[str, str],
) -> int:
    """Re-run a run from its own harness commit, docs and UI source, as a dry run.

    The harness commit must be on the target branch or in the checkout's history
    (:func:`replayable_harness`). Its harness paths are extracted into the run
    directory and mounted as ``/repo``, and the toolchain image is the one that
    commit's ``install.sh`` defines. The original run is mounted read-only at ``/replay``,
    where the orchestrator's ``--replay`` reads its flows, model, effort, budgets
    and docs commit, and the checkout read-only at ``/source``, the ``--repo-dir``
    that commit's docs are read from with git. A run that tested a build has that
    build made again from its head, in the credential-free container.
    """
    env = dict(environ)
    source = Path(args.replay).expanduser().resolve()
    plan = replay_plan(_story_record(source))
    stack = args.stack or plan["stack"] or DEFAULT_STACK
    _check_stack(stack)
    if plan["stack"] and stack != plan["stack"]:
        print(
            f"WARNING: the run was against {plan['stack']}; this replays it against {stack}",
            flush=True,
        )
    _require(env, "UX_TEST_PERSONAS", "UX_TEST_AWS_ACCOUNT_ID")
    if plan["dirty"]:
        print(
            f"WARNING: the run's harness had local changes; this replays commit "
            f"{plan['harness'][:8]} without them",
            flush=True,
        )
    holder = replayable_harness(
        REPO_ROOT,
        plan["harness"],
        remote=args.remote,
        target=args.target_branch,
        run=run,
    )
    docker = resolve_tool(args.docker)
    out = story_run_dir("replay", out=args.out)
    print(f"-> run directory: {out}", flush=True)
    harness = out / "harness"
    print(
        f"-> extracting the harness at {plan['harness'][:8]}, which {holder} holds",
        flush=True,
    )
    export_tree(REPO_ROOT, plan["harness"], STORY_HARNESS_TREE, harness, run=run)
    package = harness / "scripts" / "ux_runner"
    platform_name = host_platform()
    tag = image_tag(package)
    ensure_image(
        tag, platform_name, package, docker=docker, rebuild=args.rebuild_image, run=run
    )
    credentials = export_credentials(profile=args.profile, run=run)
    region = _region(env)
    ui_env = ui_env_text(env, stack=stack, profile=args.profile, region=region, run=run)
    web_url = (
        None
        if plan["build_head"]
        else _web_url(args, ui_env, stack, region=region, run=run)
    )
    build_dir: str | None = None
    if plan["build_head"]:
        _build_mr_ui(
            docker=docker,
            platform_name=platform_name,
            tag=tag,
            out=out,
            head=plan["build_head"],
            ui_env=ui_env,
            env=env,
            run=run,
        )
        build_dir = "/out/build"
    extra = ["--video", args.video] if args.video else []
    variables = git_variables(plan["harness"])
    child_env = _container_env(env, credentials, ui_env)
    child_env.update(variables)
    print("-> replaying the run as a dry run; nothing is posted", flush=True)
    completed = run(  # noqa: S603 - fixed argv, no shell
        _review_container(
            docker=docker,
            platform_name=platform_name,
            tag=tag,
            harness=harness,
            out=out,
            names=_story_names(post=False) + tuple(variables),
            mounts=bind(source, "/replay", readonly=True)
            + bind(REPO_ROOT, "/source", readonly=True)
            + checkout_git_mounts(REPO_ROOT, run=run),
            command=_orchestrator(
                ["--replay", "/replay"],
                artifact_dir=f"/out/{STORY_ARTIFACTS}",
                stack=stack,
                web_url=web_url,
                build_dir=build_dir,
                extra=[*extra, *args.passthrough],
                repo_dir="/source",
            ),
        ),
        env=child_env,
        check=False,
    )
    return_ownership(out, docker=docker, tag=tag, platform_name=platform_name, run=run)
    report_story_run(out / STORY_ARTIFACTS)
    return completed.returncode


def _story_record(source: Path) -> Mapping[str, Any]:
    path = source / STORY_RUN_FILE
    record = _read_json(path, STORY_RECORD_BYTES, str(path))
    if not isinstance(record, dict):
        raise DockerRunError(f"{path} is not a JSON object")
    return record


def _stories_list(*, run: Callable[..., Any]) -> int:
    """Validate the catalogue, list its flows, then the leads no story covers.

    Both run on the host: neither reads anything but the catalogue and the
    CHANGELOG, so neither needs bash or Docker.
    """
    listed = run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-B", str(REPO_ROOT / STORY_LIST_ENTRY), "list"], check=False
    )
    if listed.returncode != 0:
        return listed.returncode
    print("[Unreleased] CHANGELOG leads no story covers:", flush=True)
    nudged = run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-B", str(REPO_ROOT / STORY_NUDGE_ENTRY), "nudge"], check=False
    )
    return nudged.returncode


def _stories_search(
    args: argparse.Namespace,
    *,
    run: Callable[..., Any],
    environ: Mapping[str, str],
) -> int:
    """What a first-time user finds for ``--search``, in a container with nothing.

    The index runs ``docs-site/setup.sh`` to learn which pages the site publishes,
    which needs bash and symlinks, so it runs in the image, with no network, no
    credentials and the checkout read-only.
    """
    query = " ".join(str(args.search).split())
    if not query or len(query) > SEARCH_QUERY_CHARS:
        raise DockerRunError(
            f"--search takes the words a first-time user would type, 1 to "
            f"{SEARCH_QUERY_CHARS} characters"
        )
    docker = resolve_tool(args.docker)
    platform_name = host_platform()
    tag = image_tag(PACKAGE_DIR)
    ensure_image(
        tag,
        platform_name,
        PACKAGE_DIR,
        docker=docker,
        rebuild=args.rebuild_image,
        run=run,
    )
    completed = run(  # noqa: S603 - fixed argv, no shell
        [docker, "run", "--rm", "--init", "--network", "none"]
        + ["--platform", platform_name, "--tmpfs", CONTAINER_TMPFS]
        + bind(REPO_ROOT, "/repo", readonly=True)
        + [tag, "python3", "-B", f"/repo/{STORY_SEARCH_ENTRY}", "search", "--", query],
        env=_build_env(environ),
        check=False,
    )
    return completed.returncode


def _is_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def stories(
    args: argparse.Namespace,
    *,
    run: Callable[..., Any] = subprocess.run,
    popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    environ: Mapping[str, str] | None = None,
    follow: Callable[[Sequence[str]], str | None] | None = None,
    terminal: Callable[[], bool] | None = None,
    scratch: Callable[[], Path] | None = None,
) -> int:
    """First-time-user stories in the Docker runner; the mode's exit code."""
    env = dict(os.environ if environ is None else environ)
    mode = story_mode(args)
    if mode == "list":
        return _stories_list(run=run)
    if mode == "search":
        return _stories_search(args, run=run, environ=env)
    if mode == "pair":
        return _stories_pair(
            args, run=run, environ=env, follow=follow or follow_logs, scratch=scratch
        )
    if mode == "watch":
        return _stories_watch(
            args,
            run=run,
            popen=popen,
            environ=env,
            follow=follow or follow_logs,
            terminal=terminal or _is_terminal,
        )
    if mode == "replay":
        return _stories_replay(args, run=run, environ=env)
    return _stories_run(args, run=run, popen=popen, environ=env)


def parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    """Every mode's options; unrecognised ``review`` options pass to ai_ux_test.py,
    and unrecognised ``stories`` options to ai_ux_test_stories.py.

    Abbreviations are off, so a pass-through option can never be read as a
    prefix of one of this script's own.
    """
    parser = argparse.ArgumentParser(
        prog="run_in_docker.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    modes = parser.add_subparsers(dest="mode", required=True, metavar="MODE")
    review_parser = modes.add_parser(
        "review",
        allow_abbrev=False,
        help="the CI-equivalent UX test of one MR (a dry run unless --post)",
    )
    review_parser.add_argument(
        "--mr", type=int, required=True, help="merge request iid, such as 812"
    )
    review_parser.add_argument(
        "--stack", default=DEFAULT_STACK, help="review stack (default: %(default)s)"
    )
    review_parser.add_argument(
        "--target-branch",
        default=DEFAULT_TARGET_BRANCH,
        help="the MR's target branch (default: %(default)s)",
    )
    review_parser.add_argument(
        "--remote",
        default=DEFAULT_REMOTE,
        help="git remote to fetch the MR from (default: %(default)s)",
    )
    review_parser.add_argument(
        "--out", help="output directory (default: scratch/ux-docker/mr-<iid>)"
    )
    review_parser.add_argument(
        "--profile", help="AWS CLI profile on the host (default: the CLI's own)"
    )
    review_parser.add_argument(
        "--post",
        action="store_true",
        help="post the section to the MR (default: a dry run)",
    )
    review_parser.add_argument(
        "--rebuild-image",
        action="store_true",
        help="rebuild the toolchain image even if it exists",
    )
    review_parser.add_argument(
        "--docker", default="docker", help="docker CLI (default: %(default)s)"
    )

    browser_parser = modes.add_parser(
        "browser",
        allow_abbrev=False,
        help="only the confined Chrome, proxy and recorder, DevTools on 127.0.0.1",
    )
    browser_parser.add_argument(
        "--stack", default=DEFAULT_STACK, help="review stack (default: %(default)s)"
    )
    browser_parser.add_argument(
        "--web-url",
        help="deployed UI to open, by host name: an IP address is refused "
        "(default: the stack's ApplicationWebURL output)",
    )
    browser_parser.add_argument(
        "--build-dir", help="a built UI to serve at http://localhost:3000 instead"
    )
    browser_parser.add_argument(
        "--sign-in", metavar="PERSONA", help="sign this UX_TEST_PERSONAS persona in"
    )
    browser_parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_HOST_CDP_PORT,
        help="host port for DevTools, on 127.0.0.1 only (default: %(default)s)",
    )
    browser_parser.add_argument(
        "--name",
        default=DEFAULT_BROWSER_NAME,
        help="container name (default: %(default)s)",
    )
    browser_parser.add_argument(
        "--stop", action="store_true", help="stop the browser container"
    )
    browser_parser.add_argument(
        "--profile", help="AWS CLI profile on the host (default: the CLI's own)"
    )
    browser_parser.add_argument(
        "--out", help="output directory (default: scratch/ux-docker/browser)"
    )
    browser_parser.add_argument(
        "--rebuild-image",
        action="store_true",
        help="rebuild the toolchain image even if it exists",
    )
    browser_parser.add_argument(
        "--docker", default="docker", help="docker CLI (default: %(default)s)"
    )

    stories_parser = modes.add_parser(
        "stories",
        allow_abbrev=False,
        help="first-time-user stories: unattended (a dry run unless --post), "
        "watched, paired or replayed",
    )
    stories_parser.add_argument(
        "--story", metavar="ID", help="a story of scripts/ux_stories.toml"
    )
    stories_parser.add_argument(
        "--as",
        dest="persona",
        metavar="PERSONA",
        help="one persona of the story's `as` (default: each of them, or its one)",
    )
    stories_parser.add_argument(
        "--mr",
        type=int,
        metavar="IID",
        help="run what that MR's job would select, collected on the host",
    )
    stories_parser.add_argument(
        "--all", action="store_true", help="run the nightly set of stories"
    )
    stories_parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        metavar="N",
        help="containers a --story or --all run is sharded across (default: 1)",
    )
    stories_parser.add_argument(
        "--video",
        metavar="POLICY",
        help="the orchestrator's video policy, for example never",
    )
    stories_parser.add_argument(
        "--post",
        action="store_true",
        help=f"post an --mr run's reply (default: a dry run); only on {DEFAULT_STACK}",
    )
    story_modes = stories_parser.add_mutually_exclusive_group()
    story_modes.add_argument(
        "--interactive",
        action="store_true",
        help="watch the pinned Claude TUI attempt the flow in a browser container "
        "this script starts and stops",
    )
    story_modes.add_argument(
        "--pair",
        action="store_true",
        help="start the browser and print the command for a session you start",
    )
    story_modes.add_argument(
        "--replay",
        metavar="DIR",
        help="re-run the run in DIR from its own harness commit, docs and UI",
    )
    story_modes.add_argument(
        "--list",
        action="store_true",
        help="validate the catalogue, list its flows, then the uncovered leads",
    )
    story_modes.add_argument(
        "--search",
        metavar="QUERY",
        help="what a first-time user finds in the published documentation",
    )
    stories_parser.add_argument(
        "--pair-full",
        action="store_true",
        help="--pair: your whole session, from the checkout (trusted UI and docs)",
    )
    stories_parser.add_argument(
        "--pair-keychain",
        action="store_true",
        help="--pair: without --bare, for a claude signed in through the keychain",
    )
    stories_parser.add_argument(
        "--port",
        type=int,
        help="--interactive and --pair: host port for DevTools, on 127.0.0.1 only "
        f"(default: {DEFAULT_HOST_CDP_PORT})",
    )
    stories_parser.add_argument(
        "--build-dir",
        help="a built UI to serve at http://localhost:3000 instead of the stack's",
    )
    stories_parser.add_argument(
        "--stack",
        help=f"the stack (default: {DEFAULT_STACK}, or the replayed run's)",
    )
    stories_parser.add_argument(
        "--web-url",
        help="deployed UI to open, by host name (default: UX_TEST_UI_ENV's "
        "UX_TEST_WEB_URL line, else the stack's ApplicationWebURL output)",
    )
    stories_parser.add_argument(
        "--target-branch",
        default=DEFAULT_TARGET_BRANCH,
        help="--mr: the MR's target branch; --replay: the branch a replayed "
        "harness commit must be on, unless this checkout holds it "
        "(default: %(default)s)",
    )
    stories_parser.add_argument(
        "--remote",
        default=DEFAULT_REMOTE,
        help="--mr: git remote to fetch the MR from; --replay: the remote of "
        "--target-branch (default: %(default)s)",
    )
    stories_parser.add_argument(
        "--out",
        help="the run directory (default: scratch/ux-docker/stories/<time>-<run>)",
    )
    stories_parser.add_argument(
        "--profile", help="AWS CLI profile on the host (default: the CLI's own)"
    )
    stories_parser.add_argument(
        "--name",
        default=DEFAULT_BROWSER_NAME,
        help="--interactive and --pair: the browser container (default: %(default)s)",
    )
    stories_parser.add_argument(
        "--rebuild-image",
        action="store_true",
        help="rebuild the toolchain image even if it exists",
    )
    stories_parser.add_argument(
        "--docker", default="docker", help="docker CLI (default: %(default)s)"
    )

    args, extra = parser.parse_known_args(argv)
    if args.mode == "browser" and extra:
        browser_parser.error(f"unrecognized arguments: {' '.join(extra)}")
    args.passthrough = list(extra) if args.mode in ("review", "stories") else []
    return args, extra


def main(argv: list[str] | None = None) -> int:
    """Dispatch to a mode; exit 1 with ``FAILED:`` and the reason on a host error.

    Both output streams replace what they cannot encode rather than raise: on
    Windows a redirected stream uses the ANSI code page, and the browser mode
    echoes the container's own output, which this script does not control.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    if sys.version_info < MINIMUM_PYTHON:
        print(
            f"FAILED: run_in_docker.py needs Python "
            f"{'.'.join(map(str, MINIMUM_PYTHON))} or newer, for tarfile's "
            f"filter='data'; this is {platform.python_version()}",
            file=sys.stderr,
        )
        return 1
    args, _ = parse_args(argv)
    try:
        if args.mode == "review":
            return review(args)
        if args.mode == "stories":
            return stories(args)
        return browser(args)
    except DockerRunError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
