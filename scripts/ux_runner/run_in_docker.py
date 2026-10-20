#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The MR UX test on a laptop or devbox — the CI job's toolchain, in Docker.

This is the local form of the ``ai_ux_test`` CI job. It builds (or reuses) the
image ``scripts/ux_runner/Dockerfile`` builds from ``scripts/ux_runner/install.sh``,
which is the same toolchain definition the CI job installs, so a run on a Mac
(Apple Silicon included), on Windows under Docker Desktop or on a Linux devbox
gets the same Chrome for Testing, Node, npm pins, ffmpeg, fonts, Python pins,
managed policy and users as CI does. It has two modes.

``review``
    The CI-equivalent test of one merge request, a **dry run** unless
    ``--post``. The MR is collected on the host, its ``src/ui`` is built in a
    container that holds no credentials, and the test runs in a second container.
    ``section.md``, ``report.md``, ``summary.json`` and the mp4 land in
    ``scratch/ux-docker/mr-<iid>/ux-tests/``, which is gitignored; against a
    ``--stack`` other than the review stack there is no mp4 unless
    ``--dev-video-any-stack`` is passed through (``ai_ux_test.py``'s property 6).
``browser``
    Only the confined headless Chrome, the egress proxy and the recorder, with
    DevTools published on the host's own loopback (``127.0.0.1:9222``), for an
    interactive ux-test or product-demo session on a machine with no local
    Chrome. ``--stop`` stops it and leaves the socket audit behind.

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
   ``ai_ux_test.py --collect-only`` on the host, so that script's imports have
   to hold there too. 3.11.4 is the first release whose ``tarfile`` has the
   ``filter='data'`` extraction the source export relies on.

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

Unrecognised ``review`` options pass through to ``scripts/sdlc/ai_ux_test.py``,
for example ``--max-turns 40`` or ``--persona Admin``.

Environment
-----------
AWS credentials
    Whatever the host's ``aws`` CLI resolves: ``--profile``, ``AWS_PROFILE``, a
    credential helper or an SSO session. ``AWS_REGION`` (or
    ``AWS_DEFAULT_REGION``) names the stack's region.
``UX_TEST_PERSONAS``, ``UX_TEST_AWS_ACCOUNT_ID``
    As the CI variables hold them: base64 of the persona JSON, and the account's
    twelve digits. Required by ``review``; ``browser`` needs the personas only
    for ``--sign-in``.
``UX_TEST_UI_ENV``
    Optional. Without it, the stack's ``WebUITestEnvFile`` output and the names
    of the account's buckets that start with ``<stack>-`` are read on the host.
``GITLAB_REVIEW_TOKEN``, ``CI_API_V4_URL``, ``CI_PROJECT_ID``
    ``--post`` only.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import io
import json
import os
import platform
import re
import shutil
import subprocess  # nosec B404 - fixed argv lists, never a shell
import sys
import tarfile
from collections.abc import Callable, Mapping, Sequence
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

    names = CONTAINER_ENV_NAMES + (POST_ENV_NAMES if args.post else ())
    if args.post:
        print(
            f"-> running the UX test; the result is posted to MR {args.mr}", flush=True
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
            print(f"socket audit: {audit}")
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


def parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    """Both modes' options; unrecognised ``review`` options pass to ai_ux_test.py.

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

    args, extra = parser.parse_known_args(argv)
    if args.mode == "browser" and extra:
        browser_parser.error(f"unrecognized arguments: {' '.join(extra)}")
    args.passthrough = list(extra) if args.mode == "review" else []
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
        return browser(args)
    except DockerRunError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
