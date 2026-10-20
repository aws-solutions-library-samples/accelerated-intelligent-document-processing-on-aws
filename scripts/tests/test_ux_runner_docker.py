# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for the MR UX test's pinned toolchain and its Docker runner.

Two kinds of claim are pinned here, each a way the toolchain or the local runner
could keep working while doing something unwanted:

1. **One toolchain definition, agreeing with everything that reads it.**
   ``scripts/ux_runner/install.sh`` is what both the CI job and the image run, so
   its pins must equal the ones the rest of the tree holds: the Node and npm of
   ``.gitlab-ci.yml``'s ``.node_setup``, the Claude Code of ``ai_mr_review``, the
   CI image as the Dockerfile's base, the Python versions dep-audit reads from
   ``lib/idp_common_pkg/uv.lock`` and the digests it records for the wheels the
   image installs, and the users, uids, policy directories and layout
   ``scripts/ux_runner/paths.py`` hands the browser confinement. A drift in any
   of them is a toolchain that differs between CI and a laptop, a download pip or
   ``sha256sum`` refuses, or a socket audit watching the wrong uid.
2. **No credential value is ever an argv element.** The wrapper is driven end to
   end with a fake ``run`` that records every argv and environment, so each test
   can say where a value went: into the ``docker`` subprocess's environment and
   nowhere else, never into the container that builds the MR's code, and
   DevTools published on 127.0.0.1 only.

Nothing here runs Docker, Chrome, node, npm, git or AWS. ``bash -n`` and one
throwaway ``python`` child are the only processes started.
"""

from __future__ import annotations

import ast
import base64
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlparse

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS.parent
PACKAGE = SCRIPTS / "ux_runner"
INSTALL_SH = PACKAGE / "install.sh"
DOCKERFILE = PACKAGE / "Dockerfile"
PACKAGE_JSON = PACKAGE / "package.json"
PACKAGE_LOCK = PACKAGE / "package-lock.json"
REQUIREMENTS = PACKAGE / "requirements.txt"
UV_LOCK = REPO_ROOT / "lib" / "idp_common_pkg" / "uv.lock"
GITLAB_CI = REPO_ROOT / ".gitlab-ci.yml"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import run_in_docker as rid  # noqa: E402

HEAD = "a1" * 20
CREDENTIALS_TEXT = (
    "AWS_ACCESS_KEY_ID=fake-key-id-7d2e\n"
    "AWS_SECRET_ACCESS_KEY=fake-key-material-91b0\n"
    "AWS_SESSION_TOKEN=fake-session-c44a\n"
    "AWS_CREDENTIAL_EXPIRATION=2026-10-15T12:00:00+00:00\n"
)
PERSONAS_JSON = json.dumps(
    {
        "Admin": {
            "username": "ux-persona-admin@example.invalid",
            "password": "Ux!fake-persona-value-9aZ",
        }
    }
)
STACK_ENV_FILE = (
    "VITE_USER_POOL_ID=us-east-1_AbCdEfGhI\n"
    "VITE_API_BASE_URL=https://abcde12345.execute-api.us-east-1.amazonaws.com/api\n"
    "\n"
    "VITE_INPUT_BUCKET_NAME=idp-ux-nightly-inputbucket-def456\n"
)
LISTED_BUCKETS = (
    "idp-ux-nightly-outputbucket-ghi789\tidp-ux-nightly-workingbucket-jkl012\n"
)
WEB_URL = "https://d111111abcdef8.cloudfront.net/"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _environ(**overrides: str) -> dict[str, str]:
    """A developer's host environment, holding every value the runner must hide."""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/home/dev",
        "AWS_PROFILE": "dev",
        "AWS_REGION": "us-east-1",
        "AWS_SESSION_TOKEN": "stale-session-0b1d",
        "UX_TEST_PERSONAS": _b64(PERSONAS_JSON),
        "UX_TEST_AWS_ACCOUNT_ID": "123456789012",
        "GITLAB_REVIEW_TOKEN": "fake-review-pat-55e1",
        "CI_API_V4_URL": "https://gitlab.example/api/v4",
        "CI_PROJECT_ID": "4242",
    }
    env.update(overrides)
    return env


def _hidden_values(environ: dict[str, str]) -> list[str]:
    """Every value that may travel in an environment but never in an argv."""
    persona = json.loads(PERSONAS_JSON)["Admin"]
    return [
        "fake-key-id-7d2e",
        "fake-key-material-91b0",
        "fake-session-c44a",
        "stale-session-0b1d",
        environ["UX_TEST_PERSONAS"],
        persona["username"],
        persona["password"],
        environ["UX_TEST_AWS_ACCOUNT_ID"],
        environ["GITLAB_REVIEW_TOKEN"],
    ]


def _archive(members: list[tarfile.TarInfo], bodies: list[bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for info, body in zip(members, bodies, strict=True):
            archive.addfile(info, io.BytesIO(body) if info.isfile() else None)
    return buffer.getvalue()


def _file(name: str, body: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(body)
    info.mode = 0o644
    return info, body


def _ui_archive() -> bytes:
    package, package_body = _file("src/ui/package.json", b'{"name": "ui"}\n')
    page, page_body = _file("src/ui/index.html", b"<html></html>\n")
    return _archive([package, page], [package_body, page_body])


@dataclass
class Call:
    argv: list[str]
    env: dict[str, str] | None
    kwargs: dict[str, Any]


@dataclass
class FakeHost:
    """A host with git, aws and docker, answering as each would, recording all."""

    image_present: bool = True
    review_exit: int = 0
    export_stderr: str | None = None
    detach_stderr: str | None = None
    calls: list[Call] = field(default_factory=list)

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[Any]:
        argv = [str(part) for part in argv]
        env = kwargs.get("env")
        self.calls.append(Call(argv, None if env is None else dict(env), kwargs))
        return self._answer(argv)

    def _answer(self, argv: list[str]) -> subprocess.CompletedProcess[Any]:
        tool = PurePosixPath(argv[0]).name
        if len(argv) > 1 and argv[1].endswith("ai_ux_test.py"):
            inputs = Path(argv[argv.index("--inputs") + 1])
            inputs.write_text(json.dumps({"version": 1, "head_sha": HEAD}))
            return subprocess.CompletedProcess(argv, 0)
        if tool == "git":
            return subprocess.CompletedProcess(argv, 0, _ui_archive(), b"")
        if tool == "aws" and argv[1:3] == ["configure", "export-credentials"]:
            if self.export_stderr is not None:
                return subprocess.CompletedProcess(argv, 255, "", self.export_stderr)
            return subprocess.CompletedProcess(argv, 0, CREDENTIALS_TEXT, "")
        if tool == "aws" and "describe-stacks" in argv:
            query = argv[argv.index("--query") + 1]
            text = STACK_ENV_FILE if "WebUITestEnvFile" in query else WEB_URL
            return subprocess.CompletedProcess(argv, 0, text, "")
        if tool == "aws" and "list-buckets" in argv:
            return subprocess.CompletedProcess(argv, 0, LISTED_BUCKETS, "")
        if tool == "docker" and argv[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0 if self.image_present else 1)
        if tool == "docker" and argv[1] in ("build", "stop"):
            return subprocess.CompletedProcess(argv, 0, "", "")
        if tool == "docker" and argv[1] == "run" and "bash" in argv:
            (Path(_mount_sources(argv)["/work/build"]) / "index.html").write_text("ok")
            return subprocess.CompletedProcess(argv, 0)
        if tool == "docker" and argv[1] == "run" and "-d" in argv:
            if self.detach_stderr is not None:
                return subprocess.CompletedProcess(argv, 125, "", self.detach_stderr)
            return subprocess.CompletedProcess(argv, 0, "c0ffee\n", "")
        if tool == "docker" and argv[1] == "run":
            artifacts = Path(_mount_sources(argv)["/out"]) / "ux-tests"
            artifacts.mkdir(exist_ok=True)
            (artifacts / "section.md").write_text("## UX test\n")
            (artifacts / f"ux-test-mr812-{HEAD[:8]}.mp4").write_bytes(b"\0")
            return subprocess.CompletedProcess(argv, self.review_exit)
        raise AssertionError(f"the runner ran a command no host step needs: {argv}")


def _mount_sources(argv: list[str]) -> dict[str, str]:
    """Each bind or volume target in an argv, mapped to its source."""
    mounts = {}
    for index, part in enumerate(argv):
        if part == "--mount":
            fields = dict(
                item.split("=", 1) for item in argv[index + 1].split(",") if "=" in item
            )
            mounts[fields["target"]] = fields["source"]
    return mounts


def _mount_specs(argv: list[str]) -> dict[str, str]:
    specs = {}
    for index, part in enumerate(argv):
        if part == "--mount":
            spec = argv[index + 1]
            target = next(f for f in spec.split(",") if f.startswith("target="))
            specs[target.removeprefix("target=")] = spec
    return specs


def _env_names(argv: list[str]) -> list[str]:
    return [argv[index + 1] for index, part in enumerate(argv) if part == "-e"]


def _tool(call: Call) -> str:
    return PurePosixPath(call.argv[0]).name


def _step(call: Call) -> str:
    """What one docker invocation is, in the words the assertions use."""
    verb = call.argv[1]
    if verb == "image":
        return "image inspect"
    if verb != "run":
        return verb
    if "bash" in call.argv:
        return "run build container"
    return "run browser container" if "-d" in call.argv else "run review container"


def _docker_steps(host: FakeHost) -> list[str]:
    return [_step(call) for call in host.calls if _tool(call) == "docker"]


def _only(host: FakeHost, step: str) -> Call:
    matches = [c for c in host.calls if _tool(c) == "docker" and _step(c) == step]
    assert len(matches) == 1, f"expected one {step!r}, saw {_docker_steps(host)}"
    return matches[0]


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    """A fake host, with the image inputs and every tool resolved to a fixed path."""
    package = tmp_path / "package"
    package.mkdir()
    for name in rid.IMAGE_INPUTS:
        (package / name).write_text(f"{name}\n")
    monkeypatch.setattr(rid, "PACKAGE_DIR", package)
    monkeypatch.setattr(
        rid, "resolve_tool", lambda name: f"/fake/bin/{PurePosixPath(name).name}"
    )
    return FakeHost()


def _review(
    host: FakeHost, out: Path, *extra: str, environ: dict[str, str] | None = None
) -> int:
    args, _ = rid.parse_args(["review", "--mr", "812", "--out", str(out), *extra])
    return rid.review(
        args, run=host, environ=_environ() if environ is None else environ
    )


def _gitlab_ci() -> dict[str, Any]:
    return yaml.safe_load(GITLAB_CI.read_text(encoding="utf-8"))


def _install_text() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


def _shell_value(text: str, name: str) -> str:
    match = re.search(rf'^{name}="([^"]*)"', text, re.M)
    assert match, f'install.sh has no top-level {name}="..." assignment'
    return match.group(1)


def _shell_without_heredocs(text: str) -> str:
    return re.sub(r"<<'PY'\n.*?\nPY\n", "<<'PY'\n", text, flags=re.S)


def _normalised(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# --------------------------------------------------------------------------- #
# the wrapper, driven end to end with a fake host
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_review_runs_the_image_check_then_the_build_container_then_the_review_container(
    host: FakeHost, tmp_path: Path
) -> None:
    assert _review(host, tmp_path / "out") == 0
    assert _docker_steps(host) == [
        "image inspect",
        "run build container",
        "run review container",
    ]


@pytest.mark.unit
def test_review_builds_the_image_first_when_no_image_has_its_content_tag(
    host: FakeHost, tmp_path: Path
) -> None:
    host.image_present = False
    _review(host, tmp_path / "out")
    assert _docker_steps(host) == [
        "image inspect",
        "build",
        "run build container",
        "run review container",
    ]
    build = next(c for c in host.calls if _tool(c) == "docker" and c.argv[1] == "build")
    assert build.argv[build.argv.index("--platform") + 1] == rid.host_platform()
    assert build.argv[build.argv.index("-f") + 1].endswith("Dockerfile")


@pytest.mark.unit
def test_no_credential_persona_or_token_value_appears_in_any_argv(
    host: FakeHost, tmp_path: Path
) -> None:
    environ = _environ()
    _review(host, tmp_path / "out", "--post", environ=environ)
    review = _only(host, "run review container")
    assert review.env is not None
    hidden = _hidden_values(environ) + [review.env["UX_TEST_UI_ENV"]]
    leaked = sorted(
        {
            f"{' '.join(call.argv[:3])}: {value}"
            for call in host.calls
            for part in call.argv
            for value in hidden
            if value in part
        }
    )
    assert not leaked, f"credential values reached an argv: {leaked}"


@pytest.mark.unit
def test_the_review_container_receives_every_secret_by_name_only(
    host: FakeHost, tmp_path: Path
) -> None:
    _review(host, tmp_path / "out")
    review = _only(host, "run review container")
    assert review.env is not None
    names = _env_names(review.argv)
    assert names == list(rid.CONTAINER_ENV_NAMES + rid.CI_ENV_NAMES)
    assert not [part for part in review.argv if re.match(r"^[A-Z][A-Z0-9_]*=", part)]
    assert review.env["AWS_SECRET_ACCESS_KEY"] == "fake-key-material-91b0"
    assert review.env["PYTHONUNBUFFERED"] == "1"
    assert review.env["AWS_SESSION_TOKEN"] == "fake-session-c44a", (
        "the host's stale session token reached the container beside the freshly "
        "exported keys, which is a credential that cannot authenticate"
    )
    assert (
        base64.b64decode(review.env["UX_TEST_UI_ENV"])
        .decode()
        .startswith("VITE_USER_POOL_ID=")
    )


@pytest.mark.unit
def test_the_build_container_gets_no_variable_and_its_docker_cli_no_secret(
    host: FakeHost, tmp_path: Path
) -> None:
    environ = _environ()
    _review(host, tmp_path / "out", "--post", environ=environ)
    build = _only(host, "run build container")
    assert "-e" not in build.argv
    assert not [part for part in build.argv if part.startswith(("--env", "-e="))]
    assert build.env is not None
    carried = sorted(
        name
        for name, value in build.env.items()
        if any(hidden in value for hidden in _hidden_values(environ))
    )
    assert not carried, f"the docker CLI that runs MR code was handed {carried}"
    specs = _mount_specs(build.argv)
    assert specs["/work/source"].endswith(",readonly")
    assert "readonly" not in specs["/work/build"]


def _files_holding(root: Path, values: list[str]) -> list[str]:
    """Each file under ``root`` that holds one of ``values``, named with the value."""
    return [
        f"{path.relative_to(root)}: {value}"
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
        for value in values
        if value.encode("utf-8") in path.read_bytes()
    ]


def _printed_values(capsys: pytest.CaptureFixture[str], values: list[str]) -> list[str]:
    printed = capsys.readouterr()
    return [value for value in values if value in printed.out + printed.err]


@pytest.mark.unit
def test_review_with_post_writes_and_prints_no_credential_value(
    host: FakeHost, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A value reaches the docker CLI's environment and nothing else.

    The out directory outlives the run (the source export, the UI env file, the
    inputs and the artifacts stay there), and both output streams end up in a
    terminal or a log, so neither may hold a credential, a persona value, the
    account id or the token.
    """
    environ = _environ()
    out = tmp_path / "out"
    assert _review(host, out, "--post", environ=environ) == 0
    hidden = _hidden_values(environ)
    written = [p for p in out.rglob("*") if p.is_file()]
    assert len(written) >= 5, f"the fake review wrote too little to check: {written}"
    assert not _files_holding(out, hidden)
    assert not _printed_values(capsys, hidden)


@pytest.mark.unit
def test_browser_writes_and_prints_no_credential_value(
    host: FakeHost, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = _environ()
    out = tmp_path / "out"
    args, _ = rid.parse_args(["browser", "--out", str(out), "--sign-in", "Admin"])
    ready = '{"cdp": "http://127.0.0.1:9222"}'
    assert rid.browser(args, run=host, environ=environ, follow=lambda a: ready) == 0
    hidden = _hidden_values(environ)
    assert [p for p in out.rglob("*") if p.is_file()], "nothing to check under --out"
    assert not _files_holding(out, hidden)
    assert not _printed_values(capsys, hidden)


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["review", "browser"])
def test_a_failing_host_tool_is_reported_without_a_credential_value(
    host: FakeHost,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    """The two failure paths that print a tool's own stderr, driven through main().

    A failed ``aws configure export-credentials`` and a failed ``docker run -d``
    each have their bounded stderr in the FAILED line, and an AWS CLI or Docker
    error commonly names the account in an ARN.
    """
    environ = _environ()
    account = environ["UX_TEST_AWS_ACCOUNT_ID"]
    stderr = (
        f"An error occurred for arn:aws:iam::{account}:role/dev: the session "
        "has expired"
    )
    if mode == "review":
        host.export_stderr = stderr
        real = rid.review
        monkeypatch.setattr(
            rid, "review", lambda args: real(args, run=host, environ=environ)
        )
        argv = ["review", "--mr", "812", "--out", str(tmp_path / "out")]
        reason = "`aws configure export-credentials` failed (exit 255)"
    else:
        host.detach_stderr = stderr
        real_browser = rid.browser
        monkeypatch.setattr(
            rid,
            "browser",
            lambda args: real_browser(args, run=host, environ=environ),
        )
        argv = ["browser", "--out", str(tmp_path / "out")]
        reason = "`docker run` of ux-browser failed (exit 125)"
    assert rid.main(argv) == 1
    printed = capsys.readouterr()
    assert f"FAILED: {reason}" in printed.err
    assert "arn:aws:iam::[account]:role/dev" in printed.err
    shown = printed.out + printed.err
    assert not [value for value in _hidden_values(environ) if value in shown]
    assert not _files_holding(tmp_path / "out", _hidden_values(environ))


@pytest.mark.unit
def test_the_review_container_mounts_the_repo_read_only_and_out_read_write(
    host: FakeHost, tmp_path: Path
) -> None:
    _review(host, tmp_path / "out")
    review = _only(host, "run review container")
    specs = _mount_specs(review.argv)
    assert set(specs) == {"/repo", "/out"}
    assert specs["/repo"] == f"type=bind,source={rid.REPO_ROOT},target=/repo,readonly"
    assert (
        specs["/out"] == f"type=bind,source={(tmp_path / 'out').resolve()},target=/out"
    )
    assert review.argv[review.argv.index("--tmpfs") + 1] == rid.CONTAINER_TMPFS
    assert review.argv[review.argv.index("--security-opt") + 1] == "no-new-privileges"
    assert review.argv[review.argv.index("--shm-size") + 1] == "1g"
    assert "--init" in review.argv and "--rm" in review.argv


@pytest.mark.unit
def test_review_is_a_dry_run_unless_post_and_only_post_passes_the_gitlab_names(
    host: FakeHost, tmp_path: Path
) -> None:
    _review(host, tmp_path / "dry")
    dry = _only(host, "run review container")
    host.calls.clear()
    _review(host, tmp_path / "posted", "--post")
    posted = _only(host, "run review container")
    assert "--dry-run" in dry.argv
    assert "--dry-run" not in posted.argv
    assert not set(rid.POST_ENV_NAMES) & set(_env_names(dry.argv))
    assert set(rid.POST_ENV_NAMES) <= set(_env_names(posted.argv))


@pytest.mark.unit
def test_a_ci_host_is_ci_inside_the_review_container_too(
    host: FakeHost, tmp_path: Path
) -> None:
    """``ai_ux_test.py`` refuses its developer video opt-in wherever ``CI`` or
    ``GITLAB_CI`` is set, and reads them inside the review container, where an
    ``EXTRA_ARGS`` from a CI/CD variable would deliver the opt-in without any CI
    file naming it. Both go by name, like every other variable, so the docker CLI
    sets each only where the host does, and the build container gets neither."""
    environ = _environ(CI="true", GITLAB_CI="true")
    _review(host, tmp_path / "out", "--dev-video-any-stack", environ=environ)
    review = _only(host, "run review container")
    assert review.env is not None
    forwarded = [name for name in _env_names(review.argv) if name in rid.CI_ENV_NAMES]
    assert forwarded == list(rid.CI_ENV_NAMES) == ["CI", "GITLAB_CI"]
    assert {name: review.env[name] for name in forwarded} == {
        "CI": "true",
        "GITLAB_CI": "true",
    }
    command = review.argv[review.argv.index("python3") :]
    assert command[-1] == "--dev-video-any-stack"
    assert "-e" not in _only(host, "run build container").argv


@pytest.mark.unit
def test_review_with_post_does_not_promise_a_post_an_option_can_cancel(
    host: FakeHost, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--dev-video-any-stack``, like ``--dev-unconfined``, makes the run a dry run
    whatever this script was told, and it can arrive under any abbreviation
    ``ai_ux_test.py``'s parser accepts, so the line says so rather than that it
    posts."""
    _review(host, tmp_path / "out", "--post", "--dev-v")
    printed = capsys.readouterr().out.splitlines()
    assert (
        "-> running the UX test; it posts to MR 812 unless an option passed through "
        "to it makes the run a dry run"
    ) in printed


@pytest.mark.unit
def test_review_runs_ai_ux_test_on_the_collected_inputs_with_pass_through_options_last(
    host: FakeHost, tmp_path: Path
) -> None:
    _review(host, tmp_path / "out", "--max-turns", "40", "--persona", "Admin")
    collect = host.calls[0]
    assert collect.argv[0] == sys.executable
    assert collect.argv[1] == str(rid.REPO_ROOT / "scripts" / "sdlc" / "ai_ux_test.py")
    assert collect.argv[collect.argv.index("--target-branch") + 1] == "develop"
    assert collect.argv[collect.argv.index("--remote") + 1] == "origin"
    assert "--collect-only" in collect.argv
    review = _only(host, "run review container")
    command = review.argv[review.argv.index("python3") :]
    assert command[:2] == ["python3", "/repo/scripts/sdlc/ai_ux_test.py"]
    assert command[command.index("--inputs") + 1] == "/out/inputs.json"
    assert command[command.index("--build-dir") + 1] == "/out/build"
    assert command[command.index("--artifact-dir") + 1] == "/out/ux-tests"
    assert command[command.index("--stack") + 1] == "idp-ux-nightly"
    assert command[-4:] == ["--max-turns", "40", "--persona", "Admin"]


@pytest.mark.unit
def test_review_exports_the_mr_head_and_writes_the_ui_env_beside_its_source(
    host: FakeHost, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    _review(host, out)
    archive = next(c for c in host.calls if _tool(c) == "git")
    assert archive.argv[1:] == [
        "-C",
        str(rid.REPO_ROOT),
        "archive",
        "--format=tar",
        HEAD,
        "src/ui",
    ]
    env_file = out.resolve() / "source" / "src" / "ui" / ".env.production.local"
    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "VITE_USER_POOL_ID=us-east-1_AbCdEfGhI"
    assert lines[-1] == (
        "UX_TEST_BUCKETS=idp-ux-nightly-outputbucket-ghi789,"
        "idp-ux-nightly-workingbucket-jkl012"
    )


@pytest.mark.unit
def test_review_returns_the_review_container_exit_and_names_its_outputs(
    host: FakeHost, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    host.review_exit = 3
    assert _review(host, tmp_path / "out") == 3
    printed = capsys.readouterr().out
    artifacts = (tmp_path / "out").resolve() / "ux-tests"
    assert f"section.md: {artifacts / 'section.md'}" in printed
    assert str(artifacts / f"ux-test-mr812-{HEAD[:8]}.mp4") in printed


@pytest.mark.unit
def test_review_refuses_a_missing_persona_variable_before_running_anything(
    host: FakeHost, tmp_path: Path
) -> None:
    environ = _environ()
    del environ["UX_TEST_PERSONAS"]
    with pytest.raises(rid.DockerRunError, match="UX_TEST_PERSONAS"):
        _review(host, tmp_path / "out", environ=environ)
    assert host.calls == []


@pytest.mark.unit
def test_review_with_post_refuses_a_missing_review_token_before_running_anything(
    host: FakeHost, tmp_path: Path
) -> None:
    environ = _environ()
    del environ["GITLAB_REVIEW_TOKEN"]
    with pytest.raises(rid.DockerRunError, match="GITLAB_REVIEW_TOKEN"):
        _review(host, tmp_path / "out", "--post", environ=environ)
    assert host.calls == []


@pytest.mark.unit
def test_review_refuses_an_output_directory_it_did_not_create(
    host: FakeHost, tmp_path: Path
) -> None:
    mine = tmp_path / "my-project"
    (mine / "build").mkdir(parents=True)
    (mine / "build" / "keep.txt").write_text("mine")
    with pytest.raises(rid.DockerRunError, match="not created by run_in_docker.py"):
        _review(host, mine)
    assert host.calls == []
    assert (mine / "build" / "keep.txt").read_text() == "mine"


@pytest.mark.unit
def test_prepare_out_reuses_a_directory_it_created(tmp_path: Path) -> None:
    out = rid.prepare_out(tmp_path / "fresh")
    (out / "inputs.json").write_text("{}")
    assert rid.prepare_out(tmp_path / "fresh") == out
    assert (out / rid.OUT_MARKER).is_file()


@pytest.mark.unit
def test_browser_publishes_devtools_on_host_loopback_only(
    host: FakeHost, tmp_path: Path
) -> None:
    followed: list[list[str]] = []

    def follow(argv: Any) -> str:
        followed.append(list(argv))
        return '{"cdp": "http://127.0.0.1:9222"}'

    environ = _environ()
    args, _ = rid.parse_args(
        ["browser", "--out", str(tmp_path / "out"), "--sign-in", "Admin"]
    )
    assert rid.browser(args, run=host, environ=environ, follow=follow) == 0
    started = _only(host, "run browser container")
    published = [
        started.argv[index + 1]
        for index, part in enumerate(started.argv)
        if part in ("-p", "--publish")
    ]
    assert published == ["127.0.0.1:9222:9223"]
    assert not {"-P", "--publish-all", "--network", "--net"} & set(started.argv)
    wildcard = [part for part in started.argv if "0.0.0.0" in part]
    assert wildcard == ["0.0.0.0:9223"]
    assert started.argv[started.argv.index(wildcard[0]) - 1] == "--cdp-listen"
    assert followed == [["/fake/bin/docker", "logs", "-f", "ux-browser"]]
    assert started.env is not None
    assert started.env["UX_BROWSER_HOST_PORT"] == "9222"
    assert started.env["UX_BROWSER_CONTAINER"] == "ux-browser"
    assert _env_names(started.argv) == list(
        rid.CONTAINER_ENV_NAMES + rid.BROWSER_ENV_NAMES
    )
    leaked = [v for part in started.argv for v in _hidden_values(environ) if v in part]
    assert not leaked
    command = started.argv[started.argv.index("python3") :]
    assert command[1] == "/repo/scripts/ux_runner/browser.py"
    assert command[command.index("--web-url") + 1] == WEB_URL
    assert command[command.index("--sign-in") + 1] == "Admin"


@pytest.mark.unit
def test_browser_hands_the_personas_to_the_container_only_to_sign_one_in(
    host: FakeHost, tmp_path: Path
) -> None:
    """Every process ``docker exec`` starts in the browser container inherits its
    environment, so a variable it does not need is one it should not hold."""
    args, _ = rid.parse_args(["browser", "--out", str(tmp_path / "out")])
    rid.browser(args, run=host, environ=_environ(), follow=lambda argv: '{"cdp": "x"}')
    started = _only(host, "run browser container")
    names = _env_names(started.argv)
    assert "UX_TEST_PERSONAS" not in names
    assert names == [
        name for name in rid.CONTAINER_ENV_NAMES if name != "UX_TEST_PERSONAS"
    ] + list(rid.BROWSER_ENV_NAMES)


@pytest.mark.unit
def test_browser_on_another_port_publishes_that_port_on_loopback(
    host: FakeHost, tmp_path: Path
) -> None:
    args, _ = rid.parse_args(
        ["browser", "--out", str(tmp_path / "out"), "--port", "9333"]
    )
    rid.browser(args, run=host, environ=_environ(), follow=lambda argv: '{"cdp": "x"}')
    started = _only(host, "run browser container")
    assert started.argv[started.argv.index("-p") + 1] == "127.0.0.1:9333:9223"
    assert started.env is not None
    assert started.env["UX_BROWSER_HOST_PORT"] == "9333"


@pytest.mark.unit
def test_browser_serves_a_local_build_read_only_without_reading_the_web_url(
    host: FakeHost, tmp_path: Path
) -> None:
    build = tmp_path / "ui-build"
    build.mkdir()
    (build / "index.html").write_text("<html></html>")
    args, _ = rid.parse_args(
        ["browser", "--out", str(tmp_path / "out"), "--build-dir", str(build)]
    )
    rid.browser(args, run=host, environ=_environ(), follow=lambda argv: '{"cdp": "x"}')
    started = _only(host, "run browser container")
    assert _mount_specs(started.argv)["/ui-build"].endswith(",readonly")
    command = started.argv[started.argv.index("python3") :]
    assert command[command.index("--build-dir") + 1] == "/ui-build"
    assert "--web-url" not in command
    queries = [c.argv for c in host.calls if "describe-stacks" in c.argv]
    assert all("ApplicationWebURL" not in " ".join(q) for q in queries)


@pytest.mark.unit
def test_browser_stop_runs_docker_stop_and_nothing_else(
    host: FakeHost, tmp_path: Path
) -> None:
    args, _ = rid.parse_args(["browser", "--stop", "--out", str(tmp_path / "out")])
    assert rid.browser(args, run=host, environ=_environ()) == 0
    assert [c.argv for c in host.calls] == [
        ["/fake/bin/docker", "stop", "-t", "30", "ux-browser"]
    ]


@pytest.mark.unit
def test_browser_reports_failure_when_the_container_exits_before_it_is_ready(
    host: FakeHost, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args, _ = rid.parse_args(["browser", "--out", str(tmp_path / "out")])
    assert (
        rid.browser(args, run=host, environ=_environ(), follow=lambda argv: None) == 1
    )
    assert "FAILED:" in capsys.readouterr().err


@pytest.mark.unit
def test_browser_refuses_a_container_name_docker_would_read_as_an_option(
    host: FakeHost,
) -> None:
    args, _ = rid.parse_args(["browser", "--stop", "--name=-t"])
    with pytest.raises(rid.DockerRunError, match="not a docker container name"):
        rid.browser(args, run=host, environ=_environ())
    assert host.calls == []


# --------------------------------------------------------------------------- #
# the wrapper's parts
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_bind_renders_a_windows_path_as_a_single_mount_value() -> None:
    rendered = rid.bind(PureWindowsPath(r"C:\Users\x\repo"), "/repo", readonly=True)
    assert rendered == [
        "--mount",
        r"type=bind,source=C:\Users\x\repo,target=/repo,readonly",
    ]
    assert rid.bind(PurePosixPath("/home/x/out"), "/out", readonly=False) == [
        "--mount",
        "type=bind,source=/home/x/out,target=/out",
    ]


@pytest.mark.unit
def test_bind_refuses_a_source_path_containing_a_comma() -> None:
    with pytest.raises(rid.DockerRunError, match="comma"):
        rid.bind(PurePosixPath("/home/x/a,target=/etc"), "/repo", readonly=True)


@pytest.mark.unit
def test_image_tag_changes_when_any_input_file_changes(tmp_path: Path) -> None:
    for name in rid.IMAGE_INPUTS:
        (tmp_path / name).write_text(f"{name}\n")
    first = rid.image_tag(tmp_path)
    assert re.fullmatch(r"idp-ux-runner:[0-9a-f]{12}", first)
    assert rid.image_tag(tmp_path) == first
    seen = {first}
    for name in rid.IMAGE_INPUTS:
        (tmp_path / name).write_text(f"{name}\nedited\n")
        tag = rid.image_tag(tmp_path)
        assert tag not in seen, f"editing {name} left the image tag unchanged"
        seen.add(tag)


@pytest.mark.unit
def test_image_tag_names_a_missing_input(tmp_path: Path) -> None:
    for name in rid.IMAGE_INPUTS[:-1]:
        (tmp_path / name).write_text(name)
    with pytest.raises(rid.DockerRunError, match=re.escape(rid.IMAGE_INPUTS[-1])):
        rid.image_tag(tmp_path)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("arm64", "linux/arm64"),
        ("aarch64", "linux/arm64"),
        ("ARM64", "linux/arm64"),
        ("x86_64", "linux/amd64"),
        ("AMD64", "linux/amd64"),
    ],
)
def test_host_platform_maps_the_host_cpu_to_its_docker_platform(
    machine: str, expected: str
) -> None:
    assert rid.host_platform(machine) == expected


@pytest.mark.unit
@pytest.mark.parametrize("profile", [None, "dev"])
def test_export_credentials_parses_env_no_export_and_passes_the_profile(
    monkeypatch: pytest.MonkeyPatch, profile: str | None
) -> None:
    monkeypatch.setattr(rid, "resolve_tool", lambda name: f"/fake/bin/{name}")
    seen: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        noise = "\nAWS_PROFILE=other\nnot an assignment\nAWS_SESSION_TOKEN=\n"
        return subprocess.CompletedProcess(argv, 0, CREDENTIALS_TEXT + noise, "")

    values = rid.export_credentials(profile=profile, run=run)
    assert values == {
        "AWS_ACCESS_KEY_ID": "fake-key-id-7d2e",
        "AWS_SECRET_ACCESS_KEY": "fake-key-material-91b0",
        "AWS_SESSION_TOKEN": "fake-session-c44a",
        "AWS_CREDENTIAL_EXPIRATION": "2026-10-15T12:00:00+00:00",
    }
    base = [
        "/fake/bin/aws",
        "configure",
        "export-credentials",
        "--format",
        "env-no-export",
    ]
    assert seen == [base + (["--profile", "dev"] if profile else [])]


@pytest.mark.unit
def test_export_credentials_failure_says_to_refresh_the_host_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rid, "resolve_tool", lambda name: f"/fake/bin/{name}")

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        message = "Error when retrieving token from sso: Token has expired 123456789012"
        return subprocess.CompletedProcess(argv, 255, "", message)

    with pytest.raises(rid.DockerRunError) as raised:
        rid.export_credentials(profile=None, run=run)
    assert "Refresh the host's AWS session" in str(raised.value)
    assert "123456789012" not in str(raised.value)


@pytest.mark.unit
def test_ui_env_from_the_environment_is_used_verbatim_without_an_aws_call(
    host: FakeHost,
) -> None:
    text = "VITE_USER_POOL_ID=us-east-1_AbCdEfGhI\nUX_TEST_BUCKETS=idp-ux-nightly-a\n"
    environ = {"UX_TEST_UI_ENV": _b64(text)}
    decoded = rid.ui_env_text(
        environ, stack="idp-ux-nightly", profile=None, region="us-east-1", run=host
    )
    assert decoded == text
    assert host.calls == []


@pytest.mark.unit
def test_ui_env_from_the_stack_names_exact_buckets_of_that_stack_only(
    host: FakeHost,
) -> None:
    text = rid.ui_env_text(
        {}, stack="idp-ux-nightly", profile="dev", region="us-east-1", run=host
    )
    assert text.splitlines()[-1] == (
        "UX_TEST_BUCKETS=idp-ux-nightly-outputbucket-ghi789,"
        "idp-ux-nightly-workingbucket-jkl012"
    )
    listing = next(c for c in host.calls if "list-buckets" in c.argv)
    query = listing.argv[listing.argv.index("--query") + 1]
    assert query == "Buckets[?starts_with(Name, 'idp-ux-nightly-')].Name"
    assert listing.argv[-4:] == ["--region", "us-east-1", "--profile", "dev"]


@pytest.mark.unit
def test_ui_env_that_is_not_base64_is_refused(host: FakeHost) -> None:
    with pytest.raises(rid.DockerRunError, match="not base64"):
        rid.ui_env_text(
            {"UX_TEST_UI_ENV": "VITE_X=1"},
            stack="idp-ux-nightly",
            profile=None,
            region=None,
            run=host,
        )


@pytest.mark.unit
@pytest.mark.parametrize("stack", ["idp ux", "idp-ux'; echo", "-idp", ""])
def test_review_refuses_a_stack_name_cloudformation_would_not_accept(
    host: FakeHost, tmp_path: Path, stack: str
) -> None:
    with pytest.raises(rid.DockerRunError, match="stack name"):
        _review(host, tmp_path / "out", f"--stack={stack}")
    assert host.calls == []


@pytest.mark.unit
@pytest.mark.parametrize("escape", ["parent-traversal", "absolute-symlink"])
def test_the_source_export_refuses_a_member_that_leaves_its_destination(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, escape: str
) -> None:
    if escape == "parent-traversal":
        info, body = _file("../escaped.txt", b"outside")
    else:
        info, body = tarfile.TarInfo("src/ui/link"), b""
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc"
    monkeypatch.setattr(rid, "resolve_tool", lambda name: f"/fake/bin/{name}")
    payload = _archive([info], [body])

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, payload, b"")

    destination = tmp_path / "out" / "source"
    with pytest.raises(rid.DockerRunError, match="refused"):
        rid.export_ui_source(tmp_path, HEAD, destination, run=run)
    assert not (tmp_path / "out" / "escaped.txt").exists()
    assert not (destination / "src" / "ui" / "link").is_symlink()


@pytest.mark.unit
def test_the_source_export_refuses_a_head_that_is_not_a_full_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(rid, "resolve_tool", lambda name: f"/fake/bin/{name}")

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        raise AssertionError("git ran on an unvalidated ref")

    with pytest.raises(rid.DockerRunError, match="40-hex"):
        rid.export_ui_source(tmp_path, "--output=/tmp/x", tmp_path / "source", run=run)


@pytest.mark.unit
def test_follow_logs_returns_the_ready_line_and_stops_the_follower() -> None:
    script = (
        "import time\n"
        "print('starting the egress proxy', flush=True)\n"
        'print(\'{"cdp": "http://127.0.0.1:9222"}\', flush=True)\n'
        "time.sleep(120)\n"
    )
    started: list[subprocess.Popen[str]] = []

    def popen(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
        process = subprocess.Popen(*args, **kwargs)  # noqa: S603 - fixed argv, no shell
        started.append(process)
        return process

    echoed: list[str] = []
    began = time.monotonic()
    ready = rid.follow_logs(
        [sys.executable, "-c", script], popen=popen, echo=echoed.append
    )
    assert ready is not None and json.loads(ready) == {"cdp": "http://127.0.0.1:9222"}
    assert echoed == ["starting the egress proxy"]
    assert started[0].poll() is not None, "the log follower outlived follow_logs"
    assert time.monotonic() - began < 60


@pytest.mark.unit
def test_follow_logs_returns_none_when_the_stream_ends_unready() -> None:
    script = "print('FAILED: the policy is not installed')\nprint('{\"other\": 1}')\n"
    echoed: list[str] = []
    assert rid.follow_logs([sys.executable, "-c", script], echo=echoed.append) is None
    assert echoed == ["FAILED: the policy is not installed", '{"other": 1}']


@pytest.mark.unit
def test_unknown_review_options_pass_through_and_abbreviations_are_not_expanded() -> (
    None
):
    args, extra = rid.parse_args(
        ["review", "--mr", "812", "--max-turns", "40", "--prof", "x"]
    )
    assert extra == ["--max-turns", "40", "--prof", "x"]
    assert args.passthrough == extra
    assert args.profile is None


@pytest.mark.unit
def test_unknown_browser_options_are_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        rid.parse_args(["browser", "--max-turns", "40"])
    assert "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.unit
def test_main_refuses_a_python_without_tarfile_data_filter(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(rid, "MINIMUM_PYTHON", (99, 0, 0))
    assert rid.main(["review", "--mr", "812"]) == 1
    assert "needs Python 99.0.0" in capsys.readouterr().err


@pytest.mark.unit
def test_main_turns_a_runner_error_into_exit_1_with_its_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(args: Any) -> int:
        raise rid.DockerRunError("docker is not running")

    monkeypatch.setattr(rid, "review", refuse)
    assert rid.main(["review", "--mr", "812"]) == 1
    assert "FAILED: docker is not running" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# install.sh, the one toolchain definition
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_install_sh_parses_under_bash_n() -> None:
    bash = shutil.which("bash")
    assert bash, "bash is needed for `bash -n`; every Linux and macOS runner has it"
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [bash, "-n", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.unit
def test_install_sh_fails_fast_checks_the_chrome_digest_and_installs_exact_pins() -> (
    None
):
    text = _install_text()
    required = ["set -euo pipefail", "sha256sum -c", "npm ci", "--no-deps", "useradd"]
    absent = [needle for needle in required if needle not in text]
    assert not absent, f"install.sh no longer contains {absent}"
    assert text.startswith("#!/usr/bin/env bash\n")


def _shell_code_lines(text: str) -> list[str]:
    """install.sh's command lines: heredoc bodies and comment lines dropped."""
    lines = (line.strip() for line in _shell_without_heredocs(text).splitlines())
    return [line for line in lines if line and not line.startswith("#")]


@pytest.mark.unit
def test_install_sh_leaves_the_callers_directory_before_python_or_npm_runs() -> None:
    """The CI job starts install.sh as root in the MR's checkout, beside its secrets.

    ``python3 -`` and ``python3 -m`` put the working directory first on
    ``sys.path``, and npm reads a project's ``.npmrc``, so a module or a config
    file the MR committed at its root would run inside the install. So the script
    changes into its own empty directory before the first ``python3`` or ``npm``,
    and every ``python3`` runs isolated, which also keeps ``PYTHONPATH`` and the
    user site out.
    """
    lines = _shell_code_lines(_install_text())
    made = [i for i, line in enumerate(lines) if line == 'WORK="$(mktemp -d)"']
    entered = [i for i, line in enumerate(lines) if line == 'cd "$WORK"']
    tools = [
        i for i, line in enumerate(lines) if re.search(r"\b(?:python3|npm)\b", line)
    ]
    assert made and entered and tools, (made, entered, tools)
    assert made[0] < entered[0] < tools[0], (
        f"install.sh runs {lines[tools[0]]!r} before it leaves the caller's directory"
    )
    calls = [call for line in lines for call in re.findall(r"\bpython3\b[^;&|]*", line)]
    assert len(calls) >= 2, f"the python3 scan matched too little: {calls}"
    unisolated = [call for call in calls if not re.match(r"python3\s+-[IP]\b", call)]
    assert not unisolated, f"install.sh runs python3 without -I: {unisolated}"


@pytest.mark.unit
def test_install_sh_sets_its_own_umask_before_it_creates_anything() -> None:
    """A GitLab Docker executor runs the job at umask 0000 by default, and every
    ``mkdir``, ``npm ci``, ``pip`` and ``install`` here would then leave the
    toolchain, site-packages and the policy directories writable by the browser's
    uid, which root runs code from later in the same job."""
    lines = _shell_code_lines(_install_text())
    assert lines[0] == "set -euo pipefail"
    assert lines[1] == "umask 022", lines[:3]
    creating = [
        i
        for i, line in enumerate(lines)
        if re.search(r"\b(?:mktemp|mkdir|install|apt-get|curl|npm|pip|cp|mv)\b", line)
    ]
    assert creating and creating[0] > 1


@pytest.mark.unit
def test_install_sh_writes_the_managed_policy_with_explicit_modes() -> None:
    lines = _shell_code_lines(_install_text())
    assert 'install -d -m 0755 "$DIR"' in lines
    assert 'install -m 0644 "$HERE/chrome_policy.json" "$DIR/$POLICY_NAME"' in lines
    assert not [line for line in lines if "install -D" in line], (
        "install -D gives the directories it makes no mode of their own"
    )


@pytest.mark.unit
def test_install_sh_records_a_chrome_for_testing_pin_for_both_linux_platforms() -> None:
    text = _install_text()
    version = _shell_value(text, "CFT_VERSION")
    linux64 = _shell_value(text, "CFT_SHA256_LINUX64")
    arm64 = _shell_value(text, "CFT_SHA256_LINUX_ARM64")
    malformed = [
        f"{name}={value!r}"
        for name, value in (
            ("CFT_SHA256_LINUX64", linux64),
            ("CFT_SHA256_LINUX_ARM64", arm64),
        )
        if not re.fullmatch(r"[0-9a-f]{64}", value)
    ]
    if (
        not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version)
        or int(version.split(".")[0]) < 153
    ):
        malformed.insert(0, f"CFT_VERSION={version!r}")
    assert not malformed, (
        f"install.sh carries no usable Chrome for Testing pin: {malformed}. Record "
        "the current Stable version (153 or later) that ships both linux64 and "
        "linux-arm64, and the sha256 of both of its chrome-<platform>.zip "
        "downloads, as the comment above CFT_VERSION describes."
    )
    assert linux64 != arm64, "the two platforms' zips cannot share one digest"


@pytest.mark.unit
def test_install_sh_node_and_npm_equal_the_ci_node_setup() -> None:
    setup = "\n".join(_gitlab_ci()[".node_setup"])
    node = re.search(r"nodejs\.org/dist/(v\d+\.\d+\.\d+)/node-\1-linux-", setup)
    npm = re.search(r"npm@(\d+\.\d+\.\d+)", setup)
    assert node and npm, (
        ".gitlab-ci.yml .node_setup no longer pins a Node tarball and npm"
    )
    text = _install_text()
    assert _shell_value(text, "NODE_VERSION") == node.group(1)
    assert _shell_value(text, "NPM_VERSION") == npm.group(1)
    assert (
        "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz"
        in text
    )


@pytest.mark.unit
def test_install_sh_checks_the_node_tarball_against_its_digest_before_extracting() -> (
    None
):
    """The tarball is unpacked into /usr/local as root, so it is checked first,
    against the digest the release's SHASUMS256.txt published for this arch."""
    text = _install_text()
    digests = {
        arch: _shell_value(text, f"NODE_SHA256_{arch.upper()}")
        for arch in ("x64", "arm64")
    }
    malformed = {
        a: d for a, d in digests.items() if not re.fullmatch(r"[0-9a-f]{64}", d)
    }
    assert not malformed, f"install.sh records no usable Node digest: {malformed}"
    assert digests["x64"] != digests["arm64"]
    for machine, arch in (("x86_64", "x64"), ("aarch64|arm64", "arm64")):
        row = re.search(rf"^\s*{re.escape(machine)}\)(.*);;\s*$", text, re.M)
        assert row, f"install.sh has no {machine} case"
        chosen = row.group(1)
        assert f"NODE_ARCH={arch};" in chosen, chosen
        assert f'NODE_SHA256="$NODE_SHA256_{arch.upper()}";' in chosen, chosen
    lines = _shell_code_lines(text)
    fetched = lines.index(
        'curl -fsSL "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}'
        '-linux-${NODE_ARCH}.tar.xz" -o "$WORK/node.tar.xz"'
    )
    checked = lines.index('echo "${NODE_SHA256}  $WORK/node.tar.xz" | sha256sum -c -')
    extracted = lines.index(
        'tar -xJf "$WORK/node.tar.xz" -C /usr/local --strip-components=1'
    )
    assert fetched < checked < extracted


@pytest.mark.unit
def test_install_sh_assigns_uppercase_shell_variables_only() -> None:
    tests_dir = str(SCRIPTS / "tests")
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    import exemption_discovery

    text = _install_text()
    discovered = re.findall(exemption_discovery._text_pattern("lower"), text, re.M)
    assert not discovered, f"install.sh declares a discoverable surface: {discovered}"
    shell = _shell_without_heredocs(text)
    names = re.findall(r"(?:^|[\s;])([A-Za-z_][A-Za-z0-9_]*)=", shell, re.M)
    names += re.findall(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b", shell)
    assert len(names) > 10, "the assignment scan matched almost nothing; it is broken"
    lower = sorted({name for name in names if name != name.upper()})
    assert not lower, f"install.sh assigns lower-case shell variables: {lower}"


@pytest.mark.unit
def test_install_sh_creates_the_users_uids_and_policy_paths_paths_py_names() -> None:
    from ux_runner import paths

    text = _install_text()
    assert _shell_value(text, "BROWSER_USER") == paths.BROWSER_USER
    assert int(_shell_value(text, "BROWSER_UID")) == paths.BROWSER_UID
    assert _shell_value(text, "PROXY_USER") == paths.PROXY_USER
    assert int(_shell_value(text, "PROXY_UID")) == paths.PROXY_UID
    assert _shell_value(text, "RENDER_USER") == paths.RENDER_USER
    assert int(_shell_value(text, "RENDER_UID")) == paths.RENDER_UID
    for who in ("BROWSER", "PROXY", "RENDER"):
        creates = rf'useradd --uid "\${who}_UID"[^\n]*"\${who}_USER"$'
        assert re.search(creates, text, re.M), f"no useradd for ${who}_USER"
        checked = rf'"\$\(id -u "\${who}_USER"\)" != "\${who}_UID"'
        assert re.search(checked, text), f"${who}_USER's uid is not checked"
    dirs = tuple(Path(d) for d in _shell_value(text, "POLICY_DIRS").split())
    assert dirs == tuple(paths.POLICY_INSTALL_DIRS)
    assert _shell_value(text, "POLICY_NAME") == paths.POLICY_INSTALL_NAME
    assert f'"$HERE/{paths.POLICY_FILE.name}" "$DIR/$POLICY_NAME"' in text


@pytest.mark.unit
def test_install_sh_lays_the_toolchain_out_where_paths_py_looks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ux_runner import paths

    text = _install_text()
    prefix = re.search(r'^PREFIX="\$\{([A-Z_]+):-([^}]+)\}"$', text, re.M)
    assert prefix, "install.sh no longer takes its prefix from an environment default"
    assert prefix.group(1) == paths.PREFIX_ENV
    assert Path(prefix.group(2)) == paths.DEFAULT_PREFIX
    monkeypatch.setattr(sys, "platform", "linux")
    root = Path("/prefix")
    toolchain = paths.Toolchain(root)
    for installed in (toolchain.chrome, toolchain.claude, toolchain.devtools_mcp):
        assert f'"$PREFIX/{installed.relative_to(root).as_posix()}"' in text
    assert f'prefix / "{toolchain.versions_file.name}"' in text
    assert toolchain.node_modules.relative_to(root).as_posix() == "node_modules"
    assert 'npm ci --prefix "$PREFIX"' in text


@pytest.mark.unit
def test_the_container_forwarder_port_differs_from_chromes_own_devtools_port() -> None:
    from ux_runner import paths

    assert rid.CONTAINER_CDP_PORT != paths.CDP_PORT, (
        "browser.py's forwarder and Chrome's DevTools share the container's "
        "network namespace, so they cannot listen on one port"
    )
    assert rid.CONTAINER_CDP_LISTEN == f"0.0.0.0:{rid.CONTAINER_CDP_PORT}"


@pytest.mark.unit
def test_install_sh_writes_the_versions_file_with_the_keys_the_harness_reads() -> None:
    body = re.search(r"<<'PY'\n(.*?)\nPY\n", _install_text(), re.S)
    assert body, "install.sh has no python3 heredoc writing VERSIONS.json"
    tree = ast.parse(body.group(1))
    keys = [
        key.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "versions" for t in node.targets)
        and isinstance(node.value, ast.Dict)
        for key in node.value.keys
        if isinstance(key, ast.Constant)
    ]
    assert keys == [
        "chrome",
        "chrome_platform",
        "node",
        "chrome_devtools_mcp",
        "claude_code",
        "ffmpeg",
    ]


# --------------------------------------------------------------------------- #
# Dockerfile, package.json, package-lock.json, requirements.txt
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_every_image_input_can_be_committed() -> None:
    """A gitignored input never reaches CI, whose harness is a ``git archive``.

    The root ``.gitignore`` drops every ``package-lock.json`` it does not
    negate, so without a negation (or a forced add) the lockfile stays local,
    the target branch's harness lacks it, and install.sh refuses to run.
    """
    git = shutil.which("git")
    assert git, "git is needed for `git check-ignore`; every CI image here has it"
    ignored = [
        name
        for name in rid.IMAGE_INPUTS
        if subprocess.run(  # noqa: S603 - fixed argv, no shell
            [git, "-C", str(REPO_ROOT), "check-ignore", "-q"]
            + [f"scripts/ux_runner/{name}"],
            check=False,
        ).returncode
        == 0
    ]
    assert not ignored, (
        f".gitignore drops {ignored}, so they would never be committed. Add "
        "`!scripts/ux_runner/package-lock.json` beside the other package-lock "
        "negations in .gitignore."
    )


@pytest.mark.unit
def test_dockerfile_builds_from_the_ci_image_with_every_input_and_runs_install_sh() -> (
    None
):
    lines = [
        line
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    image = _gitlab_ci()["image"]
    image = image["name"] if isinstance(image, dict) else image
    assert lines[0] == f"FROM {image}"
    copies = [line.split()[1:] for line in lines if line.startswith("COPY ")]
    assert len(copies) == 1
    *sources, destination = copies[0]
    assert sorted(sources) == sorted(n for n in rid.IMAGE_INPUTS if n != "Dockerfile")
    assert destination == "/tmp/ux-runner/"
    runs = [line for line in lines if line.startswith("RUN ")]
    assert runs and runs[0].startswith("RUN bash /tmp/ux-runner/install.sh ")
    env = next(line for line in lines if line.startswith("ENV "))
    prefix = re.search(r'^PREFIX="\$\{[A-Z_]+:-([^}]+)\}"$', _install_text(), re.M)
    assert prefix and f"UX_RUNNER_PREFIX={prefix.group(1)}" in env.split()


@pytest.mark.unit
def test_package_json_pins_the_ci_claude_code_and_chrome_devtools_mcp() -> None:
    manifest = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    claude_code = _gitlab_ci()["ai_mr_review"]["variables"]["CLAUDE_CODE_VERSION"]
    assert manifest["dependencies"] == {
        "@anthropic-ai/claude-code": claude_code,
        "chrome-devtools-mcp": "1.10.1",
    }
    assert manifest["private"] is True
    assert "devDependencies" not in manifest


@pytest.mark.unit
def test_package_lock_resolves_exactly_the_package_json_pins_from_the_public_registry() -> (
    None
):
    manifest = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    lock = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
    assert lock["lockfileVersion"] >= 2
    packages = lock["packages"]
    assert packages[""]["dependencies"] == manifest["dependencies"]
    for name, version in manifest["dependencies"].items():
        assert packages[f"node_modules/{name}"]["version"] == version
    entries = {key: entry for key, entry in packages.items() if key}
    hosts = {urlparse(entry["resolved"]).netloc for entry in entries.values()}
    assert hosts == {"registry.npmjs.org"}, f"resolved from {sorted(hosts)}"
    unverified = sorted(
        key for key, entry in entries.items() if not entry.get("integrity")
    )
    assert not unverified, f"lockfile entries without an integrity hash: {unverified}"


def _requirements() -> dict[str, tuple[str, list[str]]]:
    """requirements.txt as ``{normalised name: (version, [sha256 digest, ...])}``.

    A requirement and its ``--hash`` options are one logical line, continued with
    a trailing backslash, which is how pip reads them.
    """
    text = re.sub(r"\\\n", " ", REQUIREMENTS.read_text(encoding="utf-8"))
    entries: dict[str, tuple[str, list[str]]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        spec, *options = line.split()
        name, separator, version = spec.partition("==")
        assert separator and re.fullmatch(r"[A-Za-z0-9._-]+", name), (
            f"{spec!r} is not an exact name==version pin"
        )
        digests = [option.removeprefix("--hash=sha256:") for option in options]
        assert all(re.fullmatch(r"[0-9a-f]{64}", d) for d in digests), (
            f"{name} carries an option that is not a --hash=sha256:<digest>: {options}"
        )
        entries[_normalised(name)] = (version, digests)
    return entries


def _locked_files() -> dict[tuple[str, str], dict[str, str]]:
    """uv.lock's files: ``{(name, version): {file name: sha256 digest}}``."""
    files: dict[tuple[str, str], dict[str, str]] = {}
    for package in tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))["package"]:
        records = [package["sdist"]] if "sdist" in package else []
        records += package.get("wheels", [])
        files[(_normalised(package["name"]), package["version"])] = {
            record["url"].rsplit("/", 1)[1]: record["hash"].removeprefix("sha256:")
            for record in records
        }
    return files


@pytest.mark.unit
def test_requirements_pin_exactly_the_uv_lock_versions_of_the_nine_packages() -> None:
    locked: dict[str, set[str]] = {}
    for package in tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))["package"]:
        locked.setdefault(_normalised(package["name"]), set()).add(package["version"])
    pins = {name: version for name, (version, _) in _requirements().items()}
    assert set(pins) == {
        "boto3",
        "botocore",
        "jmespath",
        "s3transfer",
        "python-dateutil",
        "six",
        "urllib3",
        "pillow",
        "pyyaml",
    }
    drifted = {
        name: (version, sorted(locked.get(name, ())))
        for name, version in pins.items()
        if locked.get(name) != {version}
    }
    assert not drifted, (
        f"requirements.txt differs from lib/idp_common_pkg/uv.lock: {drifted}. "
        "dep-audit audits uv.lock's version, so a different pin installs an "
        "unaudited one."
    )


@pytest.mark.unit
def test_each_python_pin_carries_uv_locks_digests_for_exactly_the_images_wheels() -> (
    None
):
    """install.sh installs with ``--require-hashes``, so these digests are the only
    files pip will take. For each pin they must be uv.lock's, which are PyPI's, for
    the files the job's image installs: the universal wheel, or for a package with
    platform wheels the CPython manylinux wheel of the image's Python for each of
    x86_64 and aarch64. No source distribution is listed, because building one
    would fetch its build requirements with no digest at all."""
    image = _gitlab_ci()["image"]
    image = image["name"] if isinstance(image, dict) else image
    python = re.search(r"/python:(\d+)\.(\d+)-", image)
    assert python, f"the CI image {image!r} no longer names its Python version"
    tag = f"cp{python.group(1)}{python.group(2)}"
    locked = _locked_files()
    wrong: dict[str, object] = {}
    for name, (version, digests) in _requirements().items():
        files = locked[(name, version)]
        universal = sorted(f for f in files if f.endswith("-none-any.whl"))
        platform = [
            next(
                (
                    f
                    for f in sorted(files)
                    if re.search(rf"-{tag}-{tag}-manylinux[^-]*_{arch}\.whl$", f)
                ),
                f"<no {tag} manylinux {arch} wheel in uv.lock>",
            )
            for arch in ("x86_64", "aarch64")
        ]
        wanted = universal or platform
        expected = sorted(files.get(f, f) for f in wanted)
        if sorted(digests) != expected:
            wrong[name] = {"listed": sorted(digests), "files": wanted}
    assert not wrong, (
        f"requirements.txt's digests are not uv.lock's for the image's files: {wrong}"
    )


@pytest.mark.unit
def test_install_sh_takes_the_python_pins_only_as_the_files_they_hash() -> None:
    pips = [
        line
        for line in _shell_code_lines(_install_text())
        if re.search(r"\bpip\s+install\b", line)
    ]
    assert len(pips) == 1, pips
    for needle in ("--require-hashes", "--no-deps", '-r "$HERE/requirements.txt"'):
        assert needle in pips[0], f"the pip install has no {needle}: {pips[0]}"
