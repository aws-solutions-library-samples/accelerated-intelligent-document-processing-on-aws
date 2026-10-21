# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests for ``run_in_docker.py stories``: the first-time-user stories in Docker.

The runner is driven end to end with a fake host that answers git, aws, docker and
the host's own Python as each would, recording every argv and environment, so each
test can say where a value went and what each container was told:

1. **No credential value is ever an argv element**, in any mode, and each
   container receives only the names its mode needs: a model's container the
   review container's names, CI's two and the git view; the pair browser no AWS
   session at all; posting's names only with ``--post``.
2. **Each mode calls the orchestrator as its parser reads it**: the selection, the
   artifact directory, ``--post`` only to post, ``--judge --audit`` and
   ``--collect-only`` on the host, every one of them read by the orchestrator's
   own ``parse_args``; and no option the runner
   sets itself, nor ``--dev-unconfined``, is ever passed through.
3. **The pair session is confined and starts outside the checkout**: its MCP
   configuration holds one server reached through ``docker exec -i ... env -i``,
   and the printed command is the one whose flags were recorded at the pin in
   ``fixtures/ux_story_transcript/probe.json``.
4. **The terminal ends with one line per flow**, built from ``results.json``'s
   closed-set words and numbers by the judge's own confirmation rule.

Nothing here runs Docker, Chrome, node, git or AWS.
"""

from __future__ import annotations

import ast
import base64
import importlib.util
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO_ROOT = SCRIPTS.parent
FIXTURES = SCRIPTS / "tests" / "fixtures" / "ux_story_transcript"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import run_in_docker as rid  # noqa: E402
from ux_runner import stories, story_verdict, tool_profiles  # noqa: E402

HEAD = "b2" * 20
MR_HEAD = "c3" * 20
WORKTREE_GIT = "/srv/main-checkout/.git"
CREDENTIALS_TEXT = (
    "AWS_ACCESS_KEY_ID=fake-key-id-4a1c\n"
    "AWS_SECRET_ACCESS_KEY=fake-key-material-77d3\n"
    "AWS_SESSION_TOKEN=fake-session-e09b\n"
    "AWS_CREDENTIAL_EXPIRATION=2026-10-20T12:00:00+00:00\n"
)
PERSONAS_JSON = json.dumps(
    {
        group: {
            "username": f"ux-persona-{group.lower()}@example.invalid",
            "password": f"Ux!fake-{group.lower()}-value-3qK",
        }
        for group in ("Admin", "Author", "Viewer")
    }
)
STACK_ENV_FILE = (
    "VITE_USER_POOL_ID=us-east-1_ZyXwVuTsR\n"
    "VITE_SETTINGS_PARAMETER=idp-ux-nightly-Settings\n"
    "VITE_INPUT_BUCKET_NAME=idp-ux-nightly-inputbucket-a1b2c3\n"
)
LISTED_BUCKETS = "idp-ux-nightly-outputbucket-d4e5f6\n"
WEB_URL = "https://d222222abcdef8.cloudfront.net/"
MODEL_TEXT = "FREE-TEXT-FROM-THE-MODEL-NEVER-PRINTED"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _environ(**overrides: str) -> dict[str, str]:
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/home/dev",
        "TERM": "xterm-256color",
        "AWS_PROFILE": "dev",
        "AWS_REGION": "us-east-1",
        "AWS_SESSION_TOKEN": "stale-session-77aa",
        "UX_TEST_PERSONAS": _b64(PERSONAS_JSON),
        "UX_TEST_AWS_ACCOUNT_ID": "123456789012",
        "GITLAB_REVIEW_TOKEN": "fake-review-pat-0c0c",
        "CI_API_V4_URL": "https://gitlab.example/api/v4",
        "CI_PROJECT_ID": "4242",
    }
    env.update(overrides)
    return env


def _hidden(environ: dict[str, str]) -> list[str]:
    persona = json.loads(PERSONAS_JSON)["Admin"]
    return [
        "fake-key-id-4a1c",
        "fake-key-material-77d3",
        "fake-session-e09b",
        "stale-session-77aa",
        environ["UX_TEST_PERSONAS"],
        persona["username"],
        persona["password"],
        environ["UX_TEST_AWS_ACCOUNT_ID"],
        environ["GITLAB_REVIEW_TOKEN"],
    ]


def _tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


def _result(
    key: str,
    verdict: str,
    *,
    attempt: int = 1,
    scale: int = 1,
    used: int | None = 9,
    budget: int | None = 30,
    hesitations: int = 1,
) -> dict[str, Any]:
    story, persona = key.split("@")
    return {
        "key": key,
        "story": story,
        "persona": persona,
        "attempt": attempt,
        "budget_scale": scale,
        "verdict": verdict,
        "actions_used": used,
        "budget": budget,
        "notes": {
            "confusion": [{"what": MODEL_TEXT, "why": MODEL_TEXT, "docs_ref": None}]
            * hesitations,
            "dead_end": [],
        },
        "terminal": {"evidence": MODEL_TEXT},
    }


def _results(*flows: dict[str, Any]) -> dict[str, Any]:
    return {"schema": "ux-test-stories/results", "version": 1, "flows": list(flows)}


def _mounts(argv: list[str]) -> dict[str, str]:
    """Each ``--mount`` target in an argv, mapped to its whole spec."""
    found = {}
    for index, part in enumerate(argv):
        if part == "--mount":
            spec = argv[index + 1]
            target = next(f for f in spec.split(",") if f.startswith("target="))
            found[target.removeprefix("target=")] = spec
    return found


def _source(spec: str) -> Path:
    return Path(
        next(f for f in spec.split(",") if f.startswith("source=")).split("=", 1)[1]
    )


def _names(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, part in enumerate(argv) if part == "-e"]


def _after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


@dataclass
class Call:
    argv: list[str]
    env: dict[str, str] | None
    kwargs: dict[str, Any]


@dataclass
class FakeHost:
    """git, aws, docker and the host's Python, answering as each would, recording all."""

    mr_ui: bool = True
    review_exit: int = 0
    worktree: bool = False
    dirty: bool = False
    pair_reply: dict[str, Any] | None = None
    holders: tuple[str, ...] = ("refs/remotes/origin/develop",)
    results: dict[str, Any] = field(
        default_factory=lambda: _results(
            _result("find-where-to-start@new-analyst", "done")
        )
    )
    calls: list[Call] = field(default_factory=list)

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[Any]:
        argv = [str(part) for part in argv]
        env = kwargs.get("env")
        self.calls.append(Call(argv, None if env is None else dict(env), kwargs))
        return self._answer(argv)

    def _done(
        self, argv: list[str], code: int = 0, out: Any = "", err: Any = ""
    ) -> Any:
        return subprocess.CompletedProcess(argv, code, out, err)

    def _answer(self, argv: list[str]) -> subprocess.CompletedProcess[Any]:
        tool = PurePosixPath(argv[0]).name
        if argv[0] == sys.executable:
            return self._python(argv)
        if tool == "git":
            return self._git(argv)
        if tool == "aws" and argv[1:3] == ["configure", "export-credentials"]:
            return self._done(argv, 0, CREDENTIALS_TEXT)
        if tool == "aws" and "describe-stacks" in argv:
            query = _after(argv, "--query")
            return self._done(
                argv, 0, STACK_ENV_FILE if "WebUITestEnvFile" in query else WEB_URL
            )
        if tool == "aws" and "list-buckets" in argv:
            return self._done(argv, 0, LISTED_BUCKETS)
        if tool == "docker":
            return self._docker(argv)
        raise AssertionError(f"the runner ran a command no host step needs: {argv}")

    def _python(self, argv: list[str]) -> subprocess.CompletedProcess[Any]:
        script = argv[2]
        if script.endswith(rid.STORIES_ENTRY):
            if "--collect-only" in argv:
                changed = [
                    {"status": "M", "path": "src/ui/src/App.tsx", "old_path": None}
                ]
                record = {
                    "schema": rid.STORY_INPUTS_SCHEMA,
                    "version": 1,
                    "mr": {
                        "head_sha": MR_HEAD,
                        "changed": changed if self.mr_ui else [],
                    },
                }
                Path(_after(argv, "--inputs")).write_text(json.dumps(record))
            elif "--judge" in argv:
                (Path(_after(argv, "--judge")) / rid.STORY_RESULTS_FILE).write_text(
                    json.dumps(self.results)
                )
            return self._done(argv)
        if script.endswith((rid.STORY_LIST_ENTRY, rid.STORY_NUDGE_ENTRY)):
            return self._done(argv)
        raise AssertionError(f"the host ran an unexpected script: {argv}")

    def _git(self, argv: list[str]) -> subprocess.CompletedProcess[Any]:
        verb = argv[3:]
        if verb[:2] == ["rev-parse", "HEAD"]:
            return self._done(argv, 0, HEAD + "\n")
        if verb[:1] == ["status"]:
            return self._done(argv, 0, " M scripts/x.py\n" if self.dirty else "")
        if verb[:2] == ["rev-parse", "--git-common-dir"]:
            return self._done(argv, 0, WORKTREE_GIT + "\n")
        if verb[:2] == ["merge-base", "--is-ancestor"]:
            return self._done(argv, 0 if verb[3] in self.holders else 1)
        if verb[:1] == ["archive"]:
            if "src/ui" in argv:
                body = {"src/ui/package.json": b"{}", "src/ui/index.html": b"<html/>"}
            else:
                body = {
                    "scripts/ux_runner/Dockerfile": b"FROM x\n",
                    "scripts/ux_runner/install.sh": b"#!/bin/sh\n",
                    "scripts/ux_runner/package.json": b"{}\n",
                    "scripts/ux_runner/package-lock.json": b"{}\n",
                    "scripts/ux_runner/requirements.txt": b"\n",
                    "scripts/ux_runner/chrome_policy.json": b"{}\n",
                    ".claude/skills/ux-test-stories.md": b"# skill\n",
                    "docs-site/setup.sh": b"#!/bin/bash\n",
                }
            return self._done(argv, 0, _tar(body), b"")
        raise AssertionError(f"unexpected git call: {argv}")

    def _docker(self, argv: list[str]) -> subprocess.CompletedProcess[Any]:
        verb = argv[1]
        if verb == "image":
            return self._done(argv, 0)
        if verb in ("build", "stop"):
            return self._done(argv, 0)
        if verb == "exec":
            if "touch" in argv:
                return self._done(argv, 0)
            if any(part.endswith(rid.STORY_PAIR_ENTRY) for part in argv):
                out = Path(self._browser_out)
                (out / "pair-prompt.md").write_text("the flow's prompt\n")
                return self._done(
                    argv, 0, "preparing\n" + json.dumps(self._reply()) + "\n"
                )
            raise AssertionError(f"unexpected docker exec: {argv}")
        if verb == "run" and "-d" in argv:
            self._browser_out = _source(_mounts(argv)["/out"])
            return self._done(argv, 0, "c0ffee\n")
        if verb == "run" and "bash" in argv:
            (_source(_mounts(argv)["/work/build"]) / "index.html").write_text("ok")
            return self._done(argv, 0)
        if verb == "run" and "chown" in argv:
            return self._done(argv, 0)
        if verb == "run" and any(p.endswith(rid.STORY_SEARCH_ENTRY) for p in argv):
            return self._done(argv, 0)
        if verb == "run":
            self._review(argv)
            return self._done(argv, self.review_exit)
        raise AssertionError(f"unexpected docker call: {argv}")

    def _review(self, argv: list[str]) -> None:
        out = _source(_mounts(argv)["/out"])
        target = _after(argv, "--artifact-dir")
        folder = out / target.removeprefix("/out/")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / rid.STORY_RESULTS_FILE).write_text(json.dumps(self.results))
        (folder / "section.md").write_text("### First-time-user stories\n")

    def _reply(self) -> dict[str, Any]:
        if self.pair_reply is not None:
            return self.pair_reply
        return {
            "key": "route-folder-to-profile@platform-admin",
            "budget": 50,
            "prompt": "/out/pair-prompt.md",
            "server": {
                "type": "stdio",
                "command": "/usr/bin/env",
                "args": ["-i", "PATH=/usr/local/bin", "HOME=/tmp/ux-pair", "python3"],
                "env": {},
            },
            "allowed_tools": list(tool_profiles.STORY_ALLOWED_TOOLS),
        }

    def docker_calls(self, verb: str) -> list[Call]:
        return [
            c
            for c in self.calls
            if PurePosixPath(c.argv[0]).name == "docker" and c.argv[1] == verb
        ]

    def review_containers(self) -> list[Call]:
        return [
            c
            for c in self.docker_calls("run")
            if "-d" not in c.argv
            and "bash" not in c.argv
            and "chown" not in c.argv
            and not any(p.endswith(rid.STORY_SEARCH_ENTRY) for p in c.argv)
        ]

    def host_scripts(self, name: str) -> list[Call]:
        return [
            c
            for c in self.calls
            if c.argv[0] == sys.executable and c.argv[2].endswith(name)
        ]


class FakeProcess:
    """A ``subprocess.Popen`` stand-in that prints its lines and exits with ``code``."""

    def __init__(
        self, argv: list[str], lines: list[str], code: int, hang: bool
    ) -> None:
        self.argv = argv
        self.stdout = io.StringIO("".join(f"{line}\n" for line in lines))
        self.code = code
        self.hang = hang
        self.returncode: int | None = None
        self.waited: list[float | None] = []
        self.killed = False
        self.terminated = False

    def wait(self, timeout: float | None = None) -> int:
        self.waited.append(timeout)
        if self.hang and len(self.waited) == 1:
            raise subprocess.TimeoutExpired(self.argv, timeout or 0)
        self.returncode = self.code
        return self.code

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


@dataclass
class FakePopen:
    hang: bool = False
    started: list[tuple[list[str], dict[str, Any]]] = field(default_factory=list)
    processes: list[FakeProcess] = field(default_factory=list)

    def __call__(self, argv: list[str], **kwargs: Any) -> FakeProcess:
        argv = [str(part) for part in argv]
        self.started.append((argv, kwargs))
        process = FakeProcess(argv, [], 0, self.hang)
        self.processes.append(process)
        return process


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    package = tmp_path / "package"
    package.mkdir()
    for name in rid.IMAGE_INPUTS:
        (package / name).write_text(f"{name}\n")
    monkeypatch.setattr(rid, "PACKAGE_DIR", package)
    monkeypatch.setattr(rid, "STORIES_OUT", tmp_path / "stories")
    monkeypatch.setattr(
        rid, "resolve_tool", lambda name: f"/fake/bin/{PurePosixPath(name).name}"
    )
    return FakeHost()


def _stories(
    host: FakeHost,
    *argv: str,
    environ: dict[str, str] | None = None,
    popen: Any = None,
    terminal: bool = True,
    scratch: Path | None = None,
    follow: Any = None,
) -> int:
    args, _ = rid.parse_args(["stories", *argv])
    return rid.stories(
        args,
        run=host,
        popen=popen or FakePopen(),
        environ=_environ() if environ is None else environ,
        follow=follow or (lambda command: '{"cdp": "http://127.0.0.1:9222"}'),
        terminal=lambda: terminal,
        scratch=(lambda: scratch) if scratch is not None else None,
    )


def _leaks(host: FakeHost, environ: dict[str, str], extra: list[str] = ()) -> list[str]:
    hidden = _hidden(environ) + list(extra)
    return sorted(
        {
            f"{' '.join(call.argv[:3])}: {value}"
            for call in host.calls
            for part in call.argv
            for value in hidden
            if value in part
        }
    )


def _run_dir(host: FakeHost) -> Path:
    runs = sorted((rid.STORIES_OUT).iterdir())
    assert len(runs) == 1, runs
    return runs[0]


# --------------------------------------------------------------------------- #
# options
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_every_option_the_skills_give_a_windows_form_for_is_a_stories_option() -> None:
    parser_options = {
        arg.value
        for node in ast.walk(
            ast.parse((SCRIPTS / "ux_runner" / "run_in_docker.py").read_text())
        )
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        for arg in node.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    }
    skill = (REPO_ROOT / ".claude" / "skills" / "ux-test-stories.md").read_text()
    cited = set(
        re.findall(
            r"run_in_docker\.py stories((?: --[a-z-]+(?: [^\s`-][^\s`]*)?)+)", skill
        )
    )
    flags = {flag for line in cited for flag in re.findall(r"--[a-z][a-z-]*", line)}
    assert flags, "the skill cites no stories option at all"
    for flag in (
        "--story",
        "--as",
        "--mr",
        "--video",
        "--post",
        "--interactive",
        "--port",
        "--replay",
        "--pair",
        "--list",
        "--search",
    ):
        assert flag in parser_options, flag
    assert flags <= parser_options, sorted(flags - parser_options)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        (["--as", "new-analyst"], "--as needs --story"),
        (["--story", "find-where-to-start", "--post"], "needs --mr"),
        (["--mr", "812", "--build-dir", "/x"], "builds the MR's own UI"),
        (["--story", "find-where-to-start", "--port", "9333"], "does not apply"),
        (["--pair"], "needs --story"),
        (["--interactive", "--mr", "812"], "does not apply"),
        (["--pair", "--story", "x", "--pair-full", "--pair-keychain"], "choose one"),
        (["--pair", "--story", "x", "--docs", "y"], "unrecognized arguments"),
        (["--list", "--story", "x"], "does not apply"),
        (["--story", "Not A Story"], "is not a story id"),
        ([], "takes one of --story"),
        (["--pair-keychain", "--story", "a"], "does not apply"),
        (
            ["--story", "a", "--interactive", "--run-usd", "0.01"],
            "--run-usd does not apply to --interactive",
        ),
        (
            ["--story", "a", "--interactive", "--reruns=0"],
            "--reruns does not apply to --interactive",
        ),
    ],
)
def test_story_mode_refuses_options_the_chosen_mode_does_not_read(
    argv: list[str], reason: str
) -> None:
    args, _ = rid.parse_args(["stories", *argv])
    with pytest.raises(rid.DockerRunError, match=re.escape(reason)):
        rid.story_mode(args)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("argv", "mode"),
    [
        (["--story", "a"], "run"),
        (["--story", "a", "--as", "b", "--docs", "abc"], "run"),
        (["--mr", "812", "--post"], "run"),
        (["--story", "a", "--interactive", "--port", "9333"], "watch"),
        (["--pair", "--story", "a", "--pair-full"], "pair"),
        (["--replay", "/x", "--reruns", "0"], "replay"),
        (["--list"], "list"),
        (["--search", "prefix mapping"], "search"),
    ],
)
def test_story_mode_names_the_mode_and_passes_unknown_options_through(
    argv: list[str], mode: str
) -> None:
    args, extra = rid.parse_args(["stories", *argv])
    assert rid.story_mode(args) == mode
    assert args.passthrough == extra


@pytest.mark.unit
@pytest.mark.parametrize(
    "passed",
    [
        ["--dev-unconfined"],
        ["--artifact-dir", "/run/elsewhere"],
        ["--artifact-dir=/run/elsewhere"],
        ["--repo-dir", "/"],
        ["--inputs", "/out/other.json"],
        ["--precheck"],
        ["--judge", "/out"],
    ],
)
@pytest.mark.parametrize(
    "selection",
    [
        ["--story", "find-where-to-start"],
        ["--story", "find-where-to-start", "--interactive"],
        ["--replay", "/x"],
    ],
)
def test_no_story_mode_passes_an_option_the_runner_sets_or_must_never_pass(
    selection: list[str], passed: list[str]
) -> None:
    """The orchestrator reads the last of a repeated option, so a run directory or
    checkout passed through would replace the runner's; and
    ``--dev-unconfined`` drops the confinement a story container is, CI or not."""
    args, _ = rid.parse_args(["stories", *selection, *passed])
    with pytest.raises(rid.DockerRunError) as refused:
        rid.story_mode(args)
    message = str(refused.value)
    if passed == ["--dev-unconfined"]:
        assert "never passed in" in message
    else:
        assert message.startswith(f"{passed[0].split('=')[0]} is not passed through")


@pytest.mark.unit
def test_every_option_the_runner_keeps_from_the_orchestrator_is_one_it_declares() -> (
    None
):
    """A renamed orchestrator option must not leave this list naming nothing."""
    orchestrator = (SCRIPTS / "sdlc" / "ai_ux_test_stories.py").read_text()
    declared = {
        arg.value
        for node in ast.walk(ast.parse(orchestrator))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        for arg in node.args
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
    }
    assert set(rid.STORY_OPTIONS_SET_HERE) <= declared
    assert list(rid.STORY_OPTIONS_SET_HERE) == sorted(rid.STORY_OPTIONS_SET_HERE)


@pytest.mark.unit
def test_two_modes_at_once_are_refused_by_the_parser(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        rid.parse_args(["stories", "--pair", "--interactive", "--story", "a"])
    assert "not allowed with" in capsys.readouterr().err


@pytest.mark.unit
def test_main_dispatches_stories_and_turns_its_error_into_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(args: Any) -> int:
        raise rid.DockerRunError("docker is not running")

    monkeypatch.setattr(rid, "stories", refuse)
    assert rid.main(["stories", "--story", "find-where-to-start"]) == 1
    assert "FAILED: docker is not running" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# the catalogue as the host reads it
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_story_facts_read_the_catalogue_as_its_strict_loader_does() -> None:
    catalogue = stories.load_catalogue()
    for story in catalogue.stories:
        facts = rid.story_facts(story.id)
        assert facts.personas == tuple(
            (persona, catalogue.persona(persona).group) for persona in story.personas
        )
        assert facts.first_visit is story.first_visit
        assert facts.flow_minutes == catalogue.limits.flow_minutes


@pytest.mark.unit
def test_choose_persona_takes_a_lone_persona_and_names_the_choices_otherwise() -> None:
    lone = rid.story_facts("find-where-to-start")
    assert rid.choose_persona(lone, None) == ("new-analyst", "Author")
    several = rid.story_facts("route-folder-to-profile")
    with pytest.raises(rid.DockerRunError, match="platform-admin, new-analyst"):
        rid.choose_persona(several, None)
    with pytest.raises(rid.DockerRunError, match="does not run as 'team-lead'"):
        rid.choose_persona(several, "team-lead")
    assert rid.choose_persona(several, "new-analyst") == ("new-analyst", "Author")


@pytest.mark.unit
def test_story_facts_name_a_catalogue_problem_and_the_command_that_explains_it(
    tmp_path: Path,
) -> None:
    broken = tmp_path / "ux_stories.toml"
    broken.write_text("version = [\n")
    with pytest.raises(rid.DockerRunError, match="ai-ux-test-stories-list"):
        rid.story_facts("find-where-to-start", broken)
    with pytest.raises(rid.DockerRunError, match="has no story 'no-such-story'"):
        rid.story_facts("no-such-story")


# --------------------------------------------------------------------------- #
# unattended runs
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_one_flow_runs_in_one_review_container_with_the_orchestrators_options(
    host: FakeHost,
) -> None:
    assert _stories(host, "--story", "find-where-to-start", "--as", "new-analyst") == 0
    (review,) = host.review_containers()
    command = review.argv[review.argv.index("python3") :]
    assert command[:3] == ["python3", "-B", f"/repo/{rid.STORIES_ENTRY}"]
    assert command[3:7] == ["--story", "find-where-to-start", "--as", "new-analyst"]
    assert _after(command, "--artifact-dir") == f"/out/{rid.STORY_ARTIFACTS}"
    assert _after(command, "--repo-dir") == "/repo"
    assert _after(command, "--stack") == "idp-ux-nightly"
    assert _after(command, "--web-url") == WEB_URL
    assert "--post" not in command and "--dry-run" not in command
    mounts = _mounts(review.argv)
    assert mounts["/repo"].endswith(",readonly")
    assert "readonly" not in mounts["/out"]
    assert review.argv[review.argv.index("--security-opt") + 1] == "no-new-privileges"
    assert _run_dir(host).name.endswith("-find-where-to-start@new-analyst")


@pytest.mark.unit
def test_a_story_container_receives_every_value_by_name_only(host: FakeHost) -> None:
    environ = _environ(CI="true", GITLAB_CI="true")
    _stories(host, "--story", "find-where-to-start", environ=environ)
    (review,) = host.review_containers()
    names = _names(review.argv)
    variables = rid.git_variables(HEAD)
    assert names == list(rid.CONTAINER_ENV_NAMES + rid.CI_ENV_NAMES + tuple(variables))
    assert not [p for p in review.argv if re.match(r"^[A-Z][A-Z0-9_]*=", p)]
    assert review.env is not None
    assert {name: review.env[name] for name in rid.CI_ENV_NAMES} == {
        "CI": "true",
        "GITLAB_CI": "true",
    }
    assert {name: review.env[name] for name in variables} == variables
    assert review.env["UX_HARNESS_SHA"] == HEAD
    assert review.env["AWS_SESSION_TOKEN"] == "fake-session-e09b"
    assert not _leaks(host, environ)


@pytest.mark.unit
def test_a_dirty_harness_is_labelled_dirty_for_the_orchestrator(host: FakeHost) -> None:
    host.dirty = True
    _stories(host, "--story", "find-where-to-start")
    (review,) = host.review_containers()
    assert review.env is not None
    assert review.env["UX_HARNESS_SHA"] == f"{HEAD}+dirty"


@pytest.mark.unit
def test_a_worktree_checkouts_shared_git_directory_is_mounted_where_it_points(
    host: FakeHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    shared = tmp_path / "main" / ".git"
    shared.mkdir(parents=True)
    checkout.mkdir()
    (checkout / ".git").write_text(f"gitdir: {shared}/worktrees/checkout\n")
    monkeypatch.setattr(rid.os, "name", "posix")

    def run(argv: list[str], **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(argv, 0, f"{shared}\n", "")

    assert rid.checkout_git_mounts(checkout, run=run) == [
        "--mount",
        f"type=bind,source={shared.resolve()},target={shared.resolve().as_posix()},readonly",
    ]
    (checkout / ".git").unlink()
    (checkout / ".git").mkdir()
    assert rid.checkout_git_mounts(checkout, run=run) == []


@pytest.mark.unit
def test_post_is_an_mr_runs_against_the_review_stack_with_the_token(
    host: FakeHost,
) -> None:
    environ = _environ()
    assert _stories(host, "--mr", "812", "--post", environ=environ) == 0
    (review,) = host.review_containers()
    command = review.argv[review.argv.index("python3") :]
    assert "--post" in command
    assert set(rid.POST_ENV_NAMES) <= set(_names(review.argv))
    assert not _leaks(host, environ)
    with pytest.raises(rid.DockerRunError, match="only a run against the review stack"):
        _stories(host, "--mr", "812", "--post", "--stack", "my-stack")
    for name in rid.POST_ENV_NAMES:
        missing = {key: value for key, value in environ.items() if key != name}
        host.calls.clear()
        with pytest.raises(rid.DockerRunError, match=name):
            _stories(host, "--mr", "812", "--post", environ=missing)
        assert not host.calls


@pytest.mark.unit
def test_a_dry_run_hands_the_container_no_review_token(host: FakeHost) -> None:
    _stories(host, "--mr", "812")
    (review,) = host.review_containers()
    assert not set(rid.POST_ENV_NAMES) & set(_names(review.argv))


@pytest.mark.unit
def test_an_mr_is_collected_on_the_host_and_its_ui_built_with_no_credentials(
    host: FakeHost,
) -> None:
    environ = _environ()
    assert _stories(host, "--mr", "812", environ=environ) == 0
    (collect,) = host.host_scripts(rid.STORIES_ENTRY)
    assert collect.argv[:3] == [
        sys.executable,
        "-B",
        str(REPO_ROOT / rid.STORIES_ENTRY),
    ]
    assert ["--mr", "812", "--collect-only"] == collect.argv[3:6]
    assert _after(collect.argv, "--repo-dir") == str(rid.REPO_ROOT)
    archive = next(
        c
        for c in host.calls
        if PurePosixPath(c.argv[0]).name == "git" and "archive" in c.argv
    )
    assert MR_HEAD in archive.argv
    build = next(c for c in host.docker_calls("run") if "bash" in c.argv)
    assert "-e" not in build.argv
    (review,) = host.review_containers()
    command = review.argv[review.argv.index("python3") :]
    assert command[3:7] == ["--mr", "812", "--inputs", "/out/inputs.json"]
    assert _after(command, "--build-dir") == "/out/build"
    assert not _leaks(host, environ)


@pytest.mark.unit
def test_an_mr_that_leaves_src_ui_alone_builds_nothing(host: FakeHost) -> None:
    host.mr_ui = False
    _stories(host, "--mr", "812")
    assert not [c for c in host.docker_calls("run") if "bash" in c.argv]
    (review,) = host.review_containers()
    assert "--build-dir" not in review.argv


# --------------------------------------------------------------------------- #
# the lines a run ends with
# --------------------------------------------------------------------------- #


SUB_KINDS = {"stuck": "no_route", "gave_up": "budget_exhausted"}


def _confirmed(*attempts: tuple[str, int, int]) -> story_verdict.Confirmation:
    """The judge's own confirmation of attempts given as (verdict, attempt, scale)."""
    results = [
        story_verdict.FlowResult.from_json(
            _full_result("a-story@a-persona", verdict, attempt, scale)
        )
        for verdict, attempt, scale in attempts
    ]
    return story_verdict.confirm(results)


def _full_result(key: str, verdict: str, attempt: int, scale: int) -> dict[str, Any]:
    return {
        "key": key,
        "story": "a-story",
        "persona": "a-persona",
        "group": "Author",
        "title": "A title",
        "expectation": "allowed",
        "kind": "read",
        "series": "0a" * 6,
        "attempt": attempt,
        "budget_scale": scale,
        "verdict": verdict,
        "sub_kind": SUB_KINDS.get(verdict),
        "flags": [],
        "met_expectation": story_verdict.VERDICT_MEETS[verdict],
        "findings": [],
        "documented_steps": 3,
        "actions_used": 9,
        "budget": 30,
        "overrun": 3.0,
        "budget_exhausted": False,
        "looks": 1,
        "searches": 0,
        "reads": 0,
        "notes": {
            "confusion": [],
            "dead_end": [],
            "injection": {
                "count": 0,
                "where": {"page": 0, "documentation": 0, "dialog": 0, "other": 0},
            },
        },
        "help_links_unresolved": [],
        "doc_addresses_guessed": 0,
        "commit_points": [],
        "declared_docs_found": False,
        "terminal": None,
        "claude": None,
        "audit": "clean",
        "refusals_by_origin": {},
        "video": None,
        "chapters": [],
    }


@pytest.mark.unit
def test_the_hosts_verdict_vocabulary_is_the_judges() -> None:
    assert rid.STORY_VERDICTS == story_verdict.VERDICT_ORDER
    assert rid.BAD_STORY_VERDICTS == story_verdict.BAD_VERDICTS
    assert set(rid.CONFIRMATION_LABELS) == set(story_verdict.CONFIRMATION_STATES)


@pytest.mark.unit
@pytest.mark.parametrize(
    "attempts",
    [
        (("done", 1, 1),),
        (("stuck", 1, 1),),
        (("stuck", 1, 1), ("stuck", 2, 1)),
        (("offered", 1, 1), ("done", 2, 1)),
        (("out_of_steps", 1, 1), ("done", 2, 2)),
        (("gave_up", 1, 1), ("stuck", 2, 2)),
    ],
)
def test_a_flows_line_shows_the_verdict_and_state_the_judge_confirms(
    attempts: tuple[tuple[str, int, int], ...],
) -> None:
    confirmation = _confirmed(*attempts)
    entries = [
        _result("a-story@a-persona", v, attempt=a, scale=s) for v, a, s in attempts
    ]
    line = rid.flow_line("a-story@a-persona", entries)
    label = rid.CONFIRMATION_LABELS[confirmation.state]
    assert label == confirmation.label
    shown = f"{confirmation.verdict} ({label})" if label else confirmation.verdict
    assert line == f"flow a-story@a-persona: {shown}; 9 of 30 steps; 1 hesitation"


@pytest.mark.unit
def test_the_run_ends_with_one_line_per_flow_and_no_text_from_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts = tmp_path / "ux-test-stories"
    artifacts.mkdir()
    (artifacts / "section.md").write_text(MODEL_TEXT)
    (artifacts / "flows" / "a@b").mkdir(parents=True)
    (artifacts / "flows" / "a@b" / "ux-story-1.mp4").write_bytes(b"\0")
    document = _results(
        _result(
            "route-folder-to-profile@platform-admin", "done", used=None, budget=None
        ),
        _result("find-where-to-start@new-analyst", "offered", hesitations=0),
        {"key": "x; rm -rf /", "attempt": 1, "verdict": "done"},
        {"key": "a@b", "attempt": 1, "verdict": MODEL_TEXT},
    )
    (artifacts / rid.STORY_RESULTS_FILE).write_text(json.dumps(document))
    rid.report_story_run(artifacts)
    printed = capsys.readouterr().out
    assert MODEL_TEXT not in printed
    lines = printed.splitlines()
    assert lines[-2:] == [
        "flow find-where-to-start@new-analyst: offered (unconfirmed (1 of 1)); 9 of 30 "
        "steps; 0 hesitations",
        "flow route-folder-to-profile@platform-admin: done; steps not counted; 1 hesitation",
    ]
    assert any("2 entries" in line and line.startswith("WARNING:") for line in lines)
    assert f"video:      {artifacts / 'flows' / 'a@b' / 'ux-story-1.mp4'}" in lines


@pytest.mark.unit
def test_the_run_ends_with_its_cost_as_the_orchestrator_words_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The last line is the summary's cost, in the words the orchestrator's own
    final line uses for the same totals; only its numbers are read."""
    summary = {
        "counted_usd": 6.40878,
        "pinned_usd": 6.29783,
        "sessions_without_usage": 1,
    }
    document = {
        **_results(_result("find-where-to-start@new-analyst", "done")),
        "summary": summary,
    }
    (tmp_path / rid.STORY_RESULTS_FILE).write_text(json.dumps(document))
    rid.report_story_run(tmp_path)
    last = capsys.readouterr().out.splitlines()[-1]
    assert last == f"cost:       {story_verdict.cost_text(summary)}"
    assert last == (
        "cost:       $6.41 counted, $6.30 at the pinned rates "
        "(1 session reported no usage)"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "summary",
    [
        None,
        "6.41",
        {"pinned_usd": 1.0, "sessions_without_usage": 0},
        {"counted_usd": "1.0", "pinned_usd": 1.0, "sessions_without_usage": 0},
        {"counted_usd": True, "pinned_usd": 1.0, "sessions_without_usage": 0},
        {"counted_usd": float("inf"), "pinned_usd": 1.0, "sessions_without_usage": 0},
        {"counted_usd": -1.0, "pinned_usd": 1.0, "sessions_without_usage": 0},
        {"counted_usd": 1.0, "pinned_usd": 1.0, "sessions_without_usage": 1.5},
    ],
)
def test_a_summary_without_readable_figures_prints_no_cost(summary: object) -> None:
    assert rid.cost_line(summary) is None


@pytest.mark.unit
def test_a_run_with_no_results_says_where_to_look(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rid.report_story_run(tmp_path / "missing")
    assert capsys.readouterr().out.strip().endswith(f"see {tmp_path / 'missing'}")


# --------------------------------------------------------------------------- #
# watch
# --------------------------------------------------------------------------- #


@pytest.fixture
def watched_record(host: FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    real = host._docker

    def docker(argv: list[str]) -> Any:
        answer = real(argv)
        if argv[1] == "run" and "-d" in argv:
            folder = _source(_mounts(argv)["/out"]) / rid.STORY_ARTIFACTS
            folder.mkdir()
            (folder / rid.STORY_RUN_FILE).write_text("{}")
        return answer

    monkeypatch.setattr(host, "_docker", docker)


@pytest.mark.unit
@pytest.mark.usefixtures("watched_record")
def test_watch_starts_and_stops_its_own_browser_and_judges_with_its_audit(
    host: FakeHost, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = _environ()
    popen = FakePopen()
    assert (
        _stories(
            host,
            "--story",
            "find-where-to-start",
            "--interactive",
            popen=popen,
            environ=environ,
        )
        == 0
    )
    (browser,) = [c for c in host.docker_calls("run") if "-d" in c.argv]
    command = browser.argv[browser.argv.index("python3") :]
    assert command[1] == "/repo/scripts/ux_runner/browser.py"
    assert _after(command, "--sign-in") == "Author"
    assert "--keep-welcome" in command
    assert _after(browser.argv, "-p") == "127.0.0.1:9222:9223"
    assert _names(browser.argv) == list(
        rid.CONTAINER_ENV_NAMES
        + rid.CI_ENV_NAMES
        + tuple(rid.git_variables(HEAD))
        + rid.BROWSER_ENV_NAMES
    )
    ((execute, kwargs),) = popen.started
    assert execute[:6] == [
        "/fake/bin/docker",
        "exec",
        "-it",
        "-e",
        "TERM",
        "ux-browser",
    ]
    session = execute[6:]
    assert session[:4] == ["python3", "-I", "-B", f"/repo/{rid.STORIES_ENTRY}"]
    assert session[4:9] == [
        "--interactive",
        "--story",
        "find-where-to-start",
        "--as",
        "new-analyst",
    ]
    flow_minutes = stories.load_catalogue().limits.flow_minutes
    assert popen.processes[0].waited[0] == flow_minutes * 60 + rid.WATCH_GRACE_SECONDS
    told = capsys.readouterr().out.splitlines()
    assert any(line.endswith(f"then type: {rid.OPENING_MESSAGE}") for line in told)
    assert len(host.docker_calls("stop")) == 1
    (judge,) = host.host_scripts(rid.STORIES_ENTRY)
    out = _run_dir(host)
    assert judge.argv[3:] == [
        "--judge",
        str(out / rid.STORY_ARTIFACTS),
        "--audit",
        str(out / "browser" / "browser-audit.json"),
    ]
    assert judge.env is not None and judge.env["UX_TEST_UI_ENV"]
    assert not any(
        "UX_TEST_PERSONAS" in part or "AWS_SECRET" in part for part in execute
    )
    assert not _leaks(host, environ)


@pytest.mark.unit
@pytest.mark.usefixtures("watched_record")
def test_watch_stops_the_container_once_when_the_wall_clock_runs_out(
    host: FakeHost, capsys: pytest.CaptureFixture[str]
) -> None:
    popen = FakePopen(hang=True)
    _stories(host, "--story", "find-where-to-start", "--interactive", popen=popen)
    assert len(host.docker_calls("stop")) == 1
    printed = capsys.readouterr().out
    assert "WARNING:" in printed and "flow_minutes" in printed


@pytest.mark.unit
def test_watch_needs_a_terminal_and_says_how_to_get_one(host: FakeHost) -> None:
    with pytest.raises(rid.DockerRunError, match="winpty"):
        _stories(
            host, "--story", "find-where-to-start", "--interactive", terminal=False
        )
    assert host.calls == []


@pytest.mark.unit
def test_watch_without_a_run_record_fails_and_judges_nothing(host: FakeHost) -> None:
    host.results = {}
    original = host._python

    def no_record(argv: list[str]) -> Any:
        raise AssertionError(f"judged a run with no record: {argv}")

    host._python = no_record  # type: ignore[method-assign]
    assert _stories(host, "--story", "find-where-to-start", "--interactive") == 1
    host._python = original  # type: ignore[method-assign]


@pytest.mark.unit
@pytest.mark.usefixtures("watched_record")
def test_watch_with_a_run_record_is_judged_on_the_host(host: FakeHost) -> None:
    assert _stories(host, "--story", "find-where-to-start", "--interactive") == 0
    assert host.host_scripts(rid.STORIES_ENTRY)


# --------------------------------------------------------------------------- #
# pair
# --------------------------------------------------------------------------- #


def _pair(
    host: FakeHost, tmp_path: Path, *extra: str, **kwargs: Any
) -> tuple[int, str]:
    scratch = tmp_path / "outside" / "ux-pair-x"
    scratch.mkdir(parents=True, exist_ok=True)
    code = _stories(
        host,
        "--pair",
        "--story",
        "route-folder-to-profile",
        "--as",
        "platform-admin",
        *extra,
        scratch=scratch,
        **kwargs,
    )
    return code, str(scratch)


@pytest.mark.unit
def test_pair_starts_a_browser_holding_no_aws_session_and_prepares_inside_it(
    host: FakeHost, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    environ = _environ()
    code, _ = _pair(host, tmp_path, environ=environ)
    assert code == 0
    (browser,) = [c for c in host.docker_calls("run") if "-d" in c.argv]
    assert _names(browser.argv) == list(rid.PAIR_ENV_NAMES + rid.BROWSER_ENV_NAMES)
    assert browser.env is not None
    assert not [name for name in browser.env if name in rid.CREDENTIAL_NAMES]
    assert "--keep-welcome" not in browser.argv
    assert _after(browser.argv[browser.argv.index("python3") :], "--sign-in") == "Admin"
    assert "/browser-out" not in _mounts(browser.argv)
    assert _after(browser.argv, "--out") == "/out/browser"
    (prepare,) = [c for c in host.docker_calls("exec") if "prepare" in c.argv]
    assert prepare.argv[prepare.argv.index("python3") :][:4] == [
        "python3",
        "-I",
        "-B",
        f"/repo/{rid.STORY_PAIR_ENTRY}",
    ]
    assert _after(prepare.argv, "--harness") == HEAD
    assert _after(prepare.argv, "--home") == rid.PAIR_HOME
    assert not _leaks(host, environ)


@pytest.mark.unit
def test_pair_writes_a_private_config_with_one_server_reached_through_env_i(
    host: FakeHost, tmp_path: Path
) -> None:
    _pair(host, tmp_path)
    config = _run_dir(host) / rid.PAIR_CONFIG
    if hasattr(os, "getuid"):
        assert config.stat().st_mode & 0o777 == 0o600
    servers = json.loads(config.read_text())["mcpServers"]
    assert list(servers) == [tool_profiles.STORY_SERVER]
    server = servers[tool_profiles.STORY_SERVER]
    assert server["command"] == "/fake/bin/docker"
    assert server["env"] == {}
    args = server["args"]
    assert args[:2] == ["exec", "-i"]
    name = args.index("ux-browser")
    assert args[name + 1 : name + 3] == ["/usr/bin/env", "-i"]
    with pytest.raises(rid.DockerRunError, match="uid:gid"):
        rid.write_pair_config(
            tmp_path / "other.json",
            docker="docker",
            name="ux-browser",
            owner="0:0 --privileged",
            server=server,
        )


def _printed_command(printed: str, start: str) -> list[str]:
    line = next(
        text.strip() for text in printed.splitlines() if text.strip().startswith(start)
    )
    return line.split()


@pytest.mark.unit
def test_pair_prints_a_confined_command_that_starts_outside_the_checkout(
    host: FakeHost, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, scratch = _pair(host, tmp_path)
    printed = capsys.readouterr().out
    words = _printed_command(printed, "cd ")
    assert words[:3] == ["cd", f"'{Path(scratch).resolve()}'", "&&"]
    command = words[3:]
    assert command[:3] == ["claude", "--bare", "--strict-mcp-config"]
    assert _after(command, "--mcp-config").strip("'") == str(
        _run_dir(host) / rid.PAIR_CONFIG
    )
    assert "--tools=" in command and "''" not in command
    granted = command[
        command.index("--allowedTools") + 1 : command.index("--permission-mode")
    ]
    assert tuple(granted) == tool_profiles.STORY_ALLOWED_TOOLS
    assert _after(command, "--permission-mode") == "manual"
    assert _after(command, "--append-system-prompt-file").strip("'") == str(
        _run_dir(host) / "pair-prompt.md"
    )
    assert f"  {rid.OPENING_MESSAGE}" in printed.splitlines()
    run = _run_dir(host)
    stop = _printed_command(printed, f"'{sys.executable}' '")
    assert stop[1:] == [
        f"'{rid.REPO_ROOT / 'scripts' / 'ux_runner' / 'run_in_docker.py'}'",
        "browser",
        "--stop",
        "--name",
        "'ux-browser'",
        "--out",
        f"'{run}'",
    ]
    judge = _printed_command(printed, f"'{sys.executable}' -B")
    assert judge[3:] == [
        "--judge",
        f"'{run / rid.STORY_ARTIFACTS}'",
        "--audit",
        f"'{run / 'browser' / 'browser-audit.json'}'",
        "--summary",
    ]


@pytest.mark.unit
def test_the_printed_pair_command_is_the_one_recorded_at_the_pin(
    tmp_path: Path,
) -> None:
    """``probe.json`` records what the pinned CLI sent for this command's flags:
    no built-in tool for ``--tools=`` and the prompt file in the system prompt."""
    spec = importlib.util.spec_from_file_location(
        "ux_story_transcript_record", FIXTURES / "record.py"
    )
    assert spec and spec.loader
    record = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(record)
    probe = json.loads((FIXTURES / "probe.json").read_text())
    assert probe["pair_command"]["builtin_tools"] == []
    assert probe["pair_command"]["system_has_marker"] is True
    recorded = [part for part in record.paired(tmp_path) if part.startswith("--")]
    printed = rid.pair_command(
        config=Path("/c.json"),
        prompt=Path("/p.md"),
        tools=[record.TOOL],
        cwd=Path("/w"),
        full=False,
        keychain=False,
        shell="posix",
    ).split(" && ", 1)[1]
    assert [part for part in printed.split() if part.startswith("--")] == recorded


@pytest.mark.unit
def test_pair_keychain_drops_bare_and_pair_full_drops_the_confinement(
    host: FakeHost, tmp_path: Path
) -> None:
    common: dict[str, Any] = {
        "config": Path("/c.json"),
        "prompt": Path("/p.md"),
        "tools": ["mcp__ux-story__report_done"],
        "cwd": Path("/w"),
        "shell": "posix",
    }
    keychain = rid.pair_command(full=False, keychain=True, **common)
    assert "--bare" not in keychain and "--strict-mcp-config" in keychain
    full = rid.pair_command(full=True, keychain=False, **common)
    for flag in ("--bare", "--strict-mcp-config", "--tools="):
        assert flag not in full
    assert (
        "--mcp-config /c.json" in full and "--append-system-prompt-file /p.md" in full
    )


def _interrupted(command: Any) -> str:
    raise KeyboardInterrupt


@pytest.mark.unit
@pytest.mark.parametrize(
    "mode",
    [
        ("--interactive", "--story", "find-where-to-start"),
        ("--pair", "--story", "route-folder-to-profile", "--as", "platform-admin"),
    ],
)
def test_an_interrupt_while_the_browser_signs_in_stops_its_container(
    host: FakeHost, tmp_path: Path, mode: tuple[str, ...]
) -> None:
    scratch = tmp_path / "outside" / "ux-pair-x"
    scratch.mkdir(parents=True)
    with pytest.raises(KeyboardInterrupt):
        _stories(host, *mode, scratch=scratch, follow=_interrupted)
    started = [c for c in host.docker_calls("run") if "-d" in c.argv]
    assert len(started) == 1
    assert [c.argv[-1] for c in host.docker_calls("stop")] == ["ux-browser"]


@pytest.mark.unit
def test_pair_full_is_refused_on_the_shared_review_stack(
    host: FakeHost, tmp_path: Path
) -> None:
    with pytest.raises(rid.DockerRunError, match="stack of your own"):
        _pair(host, tmp_path, "--pair-full")
    assert not host.docker_calls("run")
    with pytest.raises(rid.DockerRunError, match="stack of your own"):
        _pair(host, tmp_path, "--pair-full", "--stack", rid.DEFAULT_STACK)


@pytest.mark.unit
def test_the_printed_after_commands_survive_a_path_with_a_space_and_a_quote(
    tmp_path: Path,
) -> None:
    out = tmp_path / "First Last" / "it's"
    for shell in ("posix", "powershell"):
        stop, judge = rid.pair_after_commands(
            out=out, name="pair1", python="/usr/bin/python3", shell=shell
        )
        if shell == "posix":
            words = shlex.split(stop)
            assert words[words.index("--out") + 1] == str(out)
            assert words[words.index("--name") + 1] == "pair1"
            assert shlex.split(judge)[shlex.split(judge).index("--judge") + 1] == str(
                out / rid.STORY_ARTIFACTS
            )
        else:
            assert stop.startswith("& '/usr/bin/python3' ")
            assert f"--out '{str(out).replace(chr(39), chr(39) * 2)}'" in stop


@pytest.mark.unit
def test_pair_full_starts_from_the_checkout_and_warns(
    host: FakeHost, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _pair(host, tmp_path, "--pair-full", "--stack", "my-stack")
    printed = capsys.readouterr().out
    assert _printed_command(printed, "cd ")[1] == f"'{rid.REPO_ROOT}'"
    assert "WARNING: --pair-full starts your whole session" in printed


@pytest.mark.unit
def test_the_powershell_form_quotes_paths_and_runs_claude_only_after_set_location() -> (
    None
):
    command = rid.pair_command(
        config=Path("C:/Users/dev/run/pair-mcp.json"),
        prompt=Path("C:/Users/dev/it's/pair-prompt.md"),
        tools=["mcp__ux-story__report_done"],
        cwd=Path("C:/Temp/ux-pair-1"),
        full=False,
        keychain=False,
        shell="powershell",
    )
    assert command.startswith(
        "Set-Location -LiteralPath 'C:/Temp/ux-pair-1' && claude "
    )
    assert "'C:/Users/dev/it''s/pair-prompt.md'" in command
    assert "--tools= " in command


@pytest.mark.unit
def test_on_windows_both_shells_get_a_form(
    host: FakeHost,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lines = rid._pair_lines(
        out=tmp_path,
        config=tmp_path / "c.json",
        prompt=tmp_path / "p.md",
        tools=["mcp__ux-story__report_done"],
        cwd=tmp_path,
        full=False,
        keychain=False,
        key="a@b",
        group="Admin",
        stack="idp-ux-nightly",
        windows=True,
    )
    assert any(line.strip().startswith("Set-Location -LiteralPath") for line in lines)
    assert any(line.strip().startswith("cd ") for line in lines)
    stops = [line.strip() for line in lines if "browser --stop" in line]
    assert [line.startswith("& '") for line in stops] == [True, False]


@pytest.mark.unit
@pytest.mark.parametrize(
    "reply",
    [
        {"server": {"command": "/bin/sh", "args": ["-i"], "env": {}}},
        {"server": {"command": "/usr/bin/env", "args": ["-i"], "env": {"A": "1"}}},
        {"server": {"command": "/usr/bin/env", "args": ["PATH=/x"], "env": {}}},
        {"allowed_tools": ["Bash"]},
        {"allowed_tools": ["mcp__ux-story__report_done; rm -rf /"]},
        {"prompt": "/etc/passwd"},
        {"prompt": "/out/../etc/passwd"},
    ],
)
def test_a_preparer_line_of_another_shape_is_refused_and_the_browser_stopped(
    host: FakeHost, tmp_path: Path, reply: dict[str, Any]
) -> None:
    host.pair_reply = {**host._reply(), **reply}
    with pytest.raises(rid.DockerRunError, match="pair preparer"):
        _pair(host, tmp_path)
    assert len(host.docker_calls("stop")) == 1
    assert not (_run_dir(host) / rid.PAIR_CONFIG).exists()


@pytest.mark.unit
def test_pair_directory_refuses_one_inside_the_checkout() -> None:
    inside = rid.REPO_ROOT / "scratch"
    with pytest.raises(rid.DockerRunError, match="inside the checkout"):
        rid.pair_directory(lambda: inside)


@pytest.mark.unit
def test_the_pair_server_runs_as_the_developer_where_roots_files_are_foreign(
    host: FakeHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not hasattr(os, "getuid"):
        pytest.skip("a uid exists only on POSIX hosts")
    real_uid = os.getuid()
    original = host._docker

    def docker(argv: list[str]) -> Any:
        if argv[1] == "exec" and "touch" in argv:
            (Path(host._browser_out) / rid.OWNER_PROBE).write_text("")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return original(argv)

    monkeypatch.setattr(host, "_docker", docker)
    monkeypatch.setattr(rid.os, "getuid", lambda: real_uid + 1)
    _pair(host, tmp_path)
    (prepare,) = [c for c in host.docker_calls("exec") if "prepare" in c.argv]
    assert _after(prepare.argv, "--user") == f"{real_uid + 1}:{os.getgid()}"
    server = json.loads((_run_dir(host) / rid.PAIR_CONFIG).read_text())["mcpServers"]
    assert server["ux-story"]["args"][2:4] == [
        "--user",
        f"{real_uid + 1}:{os.getgid()}",
    ]
    assert not (_run_dir(host) / rid.OWNER_PROBE).exists()


# --------------------------------------------------------------------------- #
# replay, list, search, ownership
# --------------------------------------------------------------------------- #


def _run_record(**overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema": rid.STORY_RUN_SCHEMA,
        "version": 1,
        "harness_sha": "d4" * 20,
        "harness_dirty": False,
        "stack": {"name": "idp-ux-nightly"},
        "docs": {"ref": "e5" * 20},
        "ui_source": {"kind": "deployed"},
    }
    record.update(overrides)
    return record


@pytest.mark.unit
def test_replay_runs_the_runs_own_harness_from_its_commit_against_the_checkouts_git(
    host: FakeHost, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "ci-run"
    source.mkdir()
    (source / rid.STORY_RUN_FILE).write_text(
        json.dumps(_run_record(harness_dirty=True))
    )
    assert _stories(host, "--replay", str(source), "--reruns", "0") == 0
    archive = next(
        c
        for c in host.calls
        if PurePosixPath(c.argv[0]).name == "git" and "archive" in c.argv
    )
    assert archive.argv[5:] == ["d4" * 20, "--", *rid.STORY_HARNESS_TREE]
    out = _run_dir(host)
    (review,) = host.review_containers()
    mounts = _mounts(review.argv)
    assert _source(mounts["/repo"]) == out / "harness"
    assert _source(mounts["/replay"]) == source and mounts["/replay"].endswith(
        ",readonly"
    )
    assert _source(mounts["/source"]) == rid.REPO_ROOT and mounts["/source"].endswith(
        ",readonly"
    )
    command = review.argv[review.argv.index("python3") :]
    assert command[3:5] == ["--replay", "/replay"]
    assert _after(command, "--repo-dir") == "/source"
    assert command[-2:] == ["--reruns", "0"]
    assert review.env is not None and review.env["UX_HARNESS_SHA"] == "d4" * 20
    assert review.argv[review.argv.index("--tmpfs") - 1] != "-d"
    assert "WARNING: the run's harness had local changes" in capsys.readouterr().out
    assert rid.image_tag(out / "harness" / "scripts" / "ux_runner") in review.argv


@pytest.mark.unit
@pytest.mark.parametrize(
    ("holders", "holder"),
    [
        (("refs/remotes/origin/develop",), "refs/remotes/origin/develop"),
        (("HEAD",), "HEAD"),
    ],
)
def test_a_replayed_harness_must_be_merged_or_in_the_checkout(
    host: FakeHost,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    holders: tuple[str, ...],
    holder: str,
) -> None:
    source = tmp_path / "ci-run"
    source.mkdir()
    (source / rid.STORY_RUN_FILE).write_text(json.dumps(_run_record()))
    host.holders = holders
    assert _stories(host, "--replay", str(source)) == 0
    assert f"which {holder} holds" in capsys.readouterr().out
    checks = [c.argv[3:] for c in host.calls if "merge-base" in c.argv]
    assert checks[0] == [
        "merge-base",
        "--is-ancestor",
        "d4" * 20,
        "refs/remotes/origin/develop",
    ]


@pytest.mark.unit
def test_a_replay_naming_a_commit_off_the_target_branch_runs_nothing(
    host: FakeHost, tmp_path: Path
) -> None:
    source = tmp_path / "ci-run"
    source.mkdir()
    (source / rid.STORY_RUN_FILE).write_text(json.dumps(_run_record()))
    host.holders = ()
    with pytest.raises(rid.DockerRunError, match="on neither origin/develop nor"):
        _stories(host, "--replay", str(source))
    assert not [c for c in host.calls if "archive" in c.argv]
    assert not [c for c in host.calls if PurePosixPath(c.argv[0]).name == "docker"]
    assert not [c for c in host.calls if PurePosixPath(c.argv[0]).name == "aws"]
    host.holders = ("refs/remotes/upstream/main",)
    assert (
        _stories(
            host,
            "--replay",
            str(source),
            "--remote",
            "upstream",
            "--target-branch",
            "main",
        )
        == 0
    )


def _orchestrator_parser() -> Any:
    """The orchestrator's own ``parse_args``, imported for the seam check alone."""
    sdlc = str(SCRIPTS / "sdlc")
    if sdlc not in sys.path:
        sys.path.insert(0, sdlc)
    import ai_ux_test_stories  # type: ignore[import-not-found]

    return ai_ux_test_stories.parse_args


def _orchestrator_calls(host: FakeHost, popen: FakePopen) -> list[list[str]]:
    """Every orchestrator command line a mode ran, from its first option on."""
    found = []
    for argv in [call.argv for call in host.calls] + [a for a, _ in popen.started]:
        at = next(
            (i for i, part in enumerate(argv) if part.endswith(rid.STORIES_ENTRY)), None
        )
        if at is not None:
            found.append(argv[at + 1 :])
    return found


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["one-flow", "mr", "mr-post", "watch", "replay"])
def test_every_orchestrator_command_a_mode_runs_is_read_by_its_own_parser(
    mode: str, host: FakeHost, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """The runner writes each orchestrator command line and the orchestrator's own
    parser reads it, so this reads every one a mode runs with that parser: a
    renamed or newly refused option fails here, and each lands in the mode, run
    directory, checkout and stack the runner meant."""
    parse = _orchestrator_parser()
    popen = FakePopen()
    source = tmp_path / "ci-run"
    argv = {
        "one-flow": ["--story", "find-where-to-start", "--as", "new-analyst"]
        + ["--docs", "e5" * 20, "--reruns", "0"],
        "mr": ["--mr", "812"],
        "mr-post": ["--mr", "812", "--post"],
        "watch": ["--story", "find-where-to-start", "--interactive"],
        "replay": ["--replay", str(source), "--reruns", "0"],
    }[mode]
    if mode == "watch":
        request.getfixturevalue("watched_record")
    if mode == "replay":
        source.mkdir()
        (source / rid.STORY_RUN_FILE).write_text(json.dumps(_run_record()))
    assert _stories(host, *argv, popen=popen) == 0
    parsed = [parse(line) for line in _orchestrator_calls(host, popen)]
    inside = [args for args in parsed if str(args.artifact_dir).startswith("/out/")]
    on_host = [args for args in parsed if args not in inside]
    assert inside
    for args in inside:
        assert args.stack == rid.DEFAULT_STACK
        assert str(args.repo_dir) == ("/source" if mode == "replay" else "/repo")
        assert not args.dev_unconfined and args.judge is None
        assert args.post is (mode == "mr-post")
    if mode == "one-flow":
        (args,) = inside
        assert (args.story, args.as_persona) == ("find-where-to-start", "new-analyst")
        assert (args.docs, args.reruns, args.interactive) == ("e5" * 20, 0, False)
        assert str(args.artifact_dir) == f"/out/{rid.STORY_ARTIFACTS}"
        assert args.web_url == WEB_URL and args.build_dir is None
    elif mode in ("mr", "mr-post"):
        (collect,) = on_host
        assert collect.collect_only and collect.mr == 812
        assert Path(collect.repo_dir) == rid.REPO_ROOT
        (args,) = inside
        assert args.mr == 812 and not args.collect_only
        assert (str(args.inputs), str(args.build_dir)) == (
            "/out/inputs.json",
            "/out/build",
        )
    elif mode == "watch":
        (args,) = inside
        assert args.interactive and args.story == "find-where-to-start"
        assert args.as_persona == "new-analyst"
        (judge,) = on_host
        out = _run_dir(host)
        assert Path(judge.judge) == out / rid.STORY_ARTIFACTS
        assert Path(judge.audit) == out / "browser" / "browser-audit.json"
    else:
        (args,) = inside
        assert (args.replay, args.reruns) == ("/replay", 0)
        assert not on_host


@pytest.mark.unit
def test_replaying_a_build_builds_that_head_again(
    host: FakeHost, tmp_path: Path
) -> None:
    source = tmp_path / "ci-run"
    source.mkdir()
    record = _run_record(ui_source={"kind": "build", "head_sha": MR_HEAD})
    (source / rid.STORY_RUN_FILE).write_text(json.dumps(record))
    _stories(host, "--replay", str(source))
    assert [c for c in host.docker_calls("run") if "bash" in c.argv]
    (review,) = host.review_containers()
    assert _after(review.argv, "--build-dir") == "/out/build"
    assert "--web-url" not in review.argv


@pytest.mark.unit
@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (_run_record(harness_sha="unknown"), "harness_sha"),
        (_run_record(schema="other"), "is not ux-test-stories/run v1"),
        (_run_record(ui_source={"kind": "build"}), "head_sha"),
        (_run_record(ui_source={"kind": "elsewhere"}), "cannot be replayed"),
        (_run_record(mode="pair"), "a pair run"),
    ],
)
def test_replay_refuses_a_record_it_cannot_reproduce(
    record: dict[str, Any], reason: str
) -> None:
    with pytest.raises(rid.DockerRunError, match=re.escape(reason)):
        rid.replay_plan(record)


@pytest.mark.unit
def test_list_validates_and_nudges_on_the_host(
    host: FakeHost, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _stories(host, "--list") == 0
    assert [c.argv[2] for c in host.calls] == [
        str(REPO_ROOT / rid.STORY_LIST_ENTRY),
        str(REPO_ROOT / rid.STORY_NUDGE_ENTRY),
    ]
    assert "[Unreleased] CHANGELOG leads no story covers:" in capsys.readouterr().out


@pytest.mark.unit
def test_search_runs_in_a_container_with_no_network_and_no_credential(
    host: FakeHost,
) -> None:
    environ = _environ()
    assert _stories(host, "--search", "-prefix   mapping", environ=environ) == 0
    (search,) = [
        c
        for c in host.docker_calls("run")
        if any(p.endswith(rid.STORY_SEARCH_ENTRY) for p in c.argv)
    ]
    assert _after(search.argv, "--network") == "none"
    assert "-e" not in search.argv
    assert search.argv[-3:] == ["search", "--", "-prefix mapping"]
    assert _mounts(search.argv)["/repo"].endswith(",readonly")
    assert search.env is not None
    assert not [n for n in search.env if n.startswith(rid.BUILD_ENV_DROPPED_PREFIXES)]
    with pytest.raises(rid.DockerRunError, match="1 to 200"):
        _stories(host, "--search", "x" * 201)


@pytest.mark.unit
def test_files_the_containers_left_as_roots_are_handed_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not hasattr(os, "getuid"):
        pytest.skip("a uid exists only on POSIX hosts")
    (tmp_path / "flows").mkdir()
    (tmp_path / "flows" / "journal.jsonl").write_text("{}")
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    rid.return_ownership(
        tmp_path,
        docker="docker",
        tag="t",
        platform_name="p",
        run=run,
        echo=lambda s: None,
    )
    assert calls == []
    uid = os.getuid()
    monkeypatch.setattr(rid.os, "getuid", lambda: uid + 1)
    said: list[str] = []
    rid.return_ownership(
        tmp_path, docker="docker", tag="t", platform_name="p", run=run, echo=said.append
    )
    (chown,) = calls
    assert chown[:6] == ["docker", "run", "--rm", "--network", "none", "--platform"]
    assert chown[-4:] == ["chown", "-hR", f"{uid + 1}:{os.getgid()}", "/out"]
    assert "-e" not in chown
    assert said[-1].startswith("WARNING:")


@pytest.mark.unit
def test_story_run_directories_sort_by_time_and_never_share_a_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rid, "STORIES_OUT", tmp_path / "stories")
    first = rid.story_run_dir("all", out=None, clock=lambda: 1792540000.0)
    second = rid.story_run_dir("all", out=None, clock=lambda: 1792540000.0)
    later = rid.story_run_dir("all", out=None, clock=lambda: 1792540061.0)
    assert first.name == "20261020T000000Z-all" or first.name.endswith("Z-all")
    assert second.name == f"{first.name}-2"
    assert sorted([later.name, first.name, second.name])[0] == first.name
    assert (first / rid.OUT_MARKER).is_file()
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "keep.txt").write_text("mine")
    with pytest.raises(rid.DockerRunError, match="not created by run_in_docker.py"):
        rid.story_run_dir("all", out=str(mine))


@pytest.mark.unit
def test_the_runner_imports_nothing_but_the_standard_library() -> None:
    tree = ast.parse((SCRIPTS / "ux_runner" / "run_in_docker.py").read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0
    }
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, sorted(
        imported - set(sys.stdlib_module_names)
    )
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level]


# --------------------------------------------------------------------------- #
# the ways a step fails
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("version = 1\n", "no [personas] table"),
        (
            'version = 1\n[personas.p]\ngroup = "Admin"\n[[stories]]\nid = "s"\n',
            "no `as` table",
        ),
        (
            'version = 1\npersonas = {}\n[[stories]]\nid = "s"\nas = { q = "allowed" }\n',
            "has no group",
        ),
        (
            'version = 1\n[limits]\nflow_minutes = "soon"\n[personas.p]\ngroup = "Admin"\n'
            '[[stories]]\nid = "s"\nas = { p = "allowed" }\n',
            "flow_minutes",
        ),
    ],
)
def test_story_facts_name_what_is_missing(
    tmp_path: Path, text: str, reason: str
) -> None:
    path = tmp_path / "ux_stories.toml"
    path.write_text(text)
    with pytest.raises(rid.DockerRunError, match=re.escape(reason)):
        rid.story_facts("s", path)
    with pytest.raises(rid.DockerRunError, match="is not a story id"):
        rid.story_facts("S P", path)


@pytest.mark.unit
def test_a_json_record_is_read_bounded_and_regular(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text('{"a": 1}')
    assert rid._read_json(good, 100, "good") == {"a": 1}
    with pytest.raises(rid.DockerRunError, match="larger than 3 bytes"):
        rid._read_json(good, 3, "good")
    with pytest.raises(rid.DockerRunError, match="cannot be read"):
        rid._read_json(tmp_path / "absent.json", 100, "absent")
    with pytest.raises(rid.DockerRunError, match="not a regular file"):
        rid._read_json(tmp_path, 100, "a directory")
    (tmp_path / "bad.json").write_text("{")
    with pytest.raises(rid.DockerRunError, match="is not JSON"):
        rid._read_json(tmp_path / "bad.json", 100, "bad")


@pytest.mark.unit
def test_a_hundredth_run_in_one_second_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rid, "STORIES_OUT", tmp_path)
    for number in range(1, 100):
        rid.story_run_dir("all", out=None, clock=lambda: 0.0)
    with pytest.raises(rid.DockerRunError, match="99 runs"):
        rid.story_run_dir("all", out=None, clock=lambda: 0.0)


@pytest.mark.unit
def test_harness_facts_say_unknown_when_git_cannot_answer(tmp_path: Path) -> None:
    def failing(argv: list[str], **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(argv, 128, "", "fatal: not a git repository")

    def odd(argv: list[str], **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(argv, 0, "not-a-sha\n", "")

    assert rid.harness_facts(tmp_path, run=failing) == ("unknown", True)
    assert rid.harness_facts(tmp_path, run=odd) == ("unknown", True)
    assert rid.git_variables("unknown") == dict(rid.STORY_GIT_CONFIG)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("record", "reason"),
    [
        ([], "not a JSON object"),
        ({"schema": rid.STORY_INPUTS_SCHEMA, "mr": "x"}, "no MR record"),
        ({"head_sha": "abc", "changed": []}, "40-hex head_sha"),
        ({"head_sha": MR_HEAD}, "no list of changed files"),
    ],
)
def test_story_inputs_of_another_shape_are_refused(
    tmp_path: Path, record: Any, reason: str
) -> None:
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(record))
    with pytest.raises(rid.DockerRunError, match=re.escape(reason)):
        rid.read_story_inputs(path)


@pytest.mark.unit
def test_a_ui_file_moved_out_of_src_ui_still_counts_as_a_ui_change(
    tmp_path: Path,
) -> None:
    path = tmp_path / "inputs.json"
    moved = {"status": "R", "path": "docs/x.md", "old_path": "src/ui/x.md"}
    path.write_text(json.dumps({"head_sha": MR_HEAD, "changed": [moved, "noise"]}))
    assert rid.read_story_inputs(path) == (MR_HEAD, True)


@pytest.mark.unit
def test_a_host_with_no_uid_hands_nothing_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(rid.os, "getuid", raising=False)

    def run(argv: list[str], **kwargs: Any) -> Any:
        raise AssertionError(f"ran {argv} on a host with no uid")

    rid.return_ownership(tmp_path, docker="d", tag="t", platform_name="p", run=run)
    assert rid.owner_probe("d", "n", tmp_path, run=run) is None


@pytest.mark.unit
def test_an_unreadable_directory_counts_as_foreign(tmp_path: Path) -> None:
    if not hasattr(os, "getuid") or os.getuid() == 0:
        pytest.skip("needs a POSIX user that a mode can lock out")
    locked = tmp_path / "locked"
    (locked / "inner").mkdir(parents=True)
    locked.chmod(0o000)
    try:
        assert rid._foreign_entries(tmp_path, os.getuid()) is True
    finally:
        locked.chmod(0o755)
    assert rid._foreign_entries(tmp_path, os.getuid()) is False


@pytest.mark.unit
def test_a_probe_that_never_landed_leaves_the_server_as_root(tmp_path: Path) -> None:
    def run(argv: list[str], **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(argv, 1, "", "")

    assert rid.owner_probe("docker", "ux-browser", tmp_path, run=run) is None


@pytest.mark.unit
def test_unreadable_results_are_a_warning_not_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / rid.STORY_RESULTS_FILE).write_text("{")
    rid.report_story_run(tmp_path)
    assert capsys.readouterr().out.strip().startswith("WARNING:")


@pytest.mark.unit
def test_the_ui_envs_web_url_line_saves_a_stack_lookup(host: FakeHost) -> None:
    environ = _environ(
        UX_TEST_UI_ENV=_b64(STACK_ENV_FILE + "UX_TEST_WEB_URL=https://ui.example/\n")
    )
    _stories(host, "--story", "find-where-to-start", environ=environ)
    (review,) = host.review_containers()
    assert _after(review.argv, "--web-url") == "https://ui.example/"
    assert not [c for c in host.calls if "describe-stacks" in c.argv]


@pytest.mark.unit
def test_a_local_build_is_served_read_only(host: FakeHost, tmp_path: Path) -> None:
    build = tmp_path / "ui-build"
    build.mkdir()
    with pytest.raises(rid.DockerRunError, match="has no index.html"):
        _stories(host, "--story", "find-where-to-start", "--build-dir", str(build))
    (build / "index.html").write_text("<html/>")
    _stories(host, "--story", "find-where-to-start", "--build-dir", str(build))
    (review,) = host.review_containers()
    assert _mounts(review.argv)["/ui-build"].endswith(",readonly")
    assert _after(review.argv, "--build-dir") == "/ui-build"
    assert "--web-url" not in review.argv
    assert not [c for c in host.calls if "ApplicationWebURL" in " ".join(c.argv)]


@pytest.mark.unit
def test_a_failed_collection_stops_the_run(host: FakeHost) -> None:
    real = host._python

    def failing(argv: list[str]) -> Any:
        if "--collect-only" in argv:
            return subprocess.CompletedProcess(argv, 1)
        return real(argv)

    host._python = failing  # type: ignore[method-assign]
    with pytest.raises(rid.DockerRunError, match="collecting MR 812 failed"):
        _stories(host, "--mr", "812")


@pytest.mark.unit
def test_a_browser_that_will_not_start_is_reported_and_one_that_dies_is_stopped(
    host: FakeHost, tmp_path: Path
) -> None:
    def refused(argv: list[str], **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(
            argv, 125, "", "Conflict. The name is in use"
        )

    with pytest.raises(rid.DockerRunError, match="If the name is taken"):
        rid._start_browser(
            ["docker", "run"],
            name="ux-browser",
            docker="docker",
            env={},
            run=refused,
            follow=lambda a: None,
        )
    stopped: list[list[str]] = []

    def started(argv: list[str], **kwargs: Any) -> Any:
        stopped.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(rid.DockerRunError, match="exited before it was ready"):
        rid._start_browser(
            ["docker", "run"],
            name="ux-browser",
            docker="docker",
            env={},
            run=started,
            follow=lambda a: None,
        )
    assert stopped[-1] == ["docker", "stop", "-t", "30", "ux-browser"]


@pytest.mark.unit
@pytest.mark.parametrize("mode", ["--interactive", "--pair"])
def test_watch_and_pair_refuse_a_container_name_docker_would_misread(
    host: FakeHost, mode: str
) -> None:
    with pytest.raises(rid.DockerRunError, match="not a docker container name"):
        _stories(host, "--story", "find-where-to-start", mode, "--name=-t")
    assert host.calls == []


@pytest.mark.unit
def test_watch_serves_a_local_build_and_kills_a_session_that_outlives_its_stop(
    host: FakeHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build = tmp_path / "ui-build"
    build.mkdir()
    (build / "index.html").write_text("<html/>")

    class Endless(FakeProcess):
        def wait(self, timeout: float | None = None) -> int:
            self.waited.append(timeout)
            if not self.killed:
                raise subprocess.TimeoutExpired(self.argv, timeout or 0)
            return -9

    made: list[Endless] = []

    def popen(argv: list[str], **kwargs: Any) -> Endless:
        made.append(Endless(argv, [], 0, False))
        return made[-1]

    code = _stories(
        host,
        "--story",
        "find-where-to-start",
        "--interactive",
        "--build-dir",
        str(build),
        popen=popen,
    )
    assert code == -9 and made[0].killed
    (browser,) = [c for c in host.docker_calls("run") if "-d" in c.argv]
    assert _mounts(browser.argv)["/ui-build"].endswith(",readonly")
    assert "--web-url" not in browser.argv
    assert _after(made[0].argv, "--build-dir") == "/ui-build"


@pytest.mark.unit
def test_a_preparer_that_prints_no_line_or_no_prompt_is_refused(tmp_path: Path) -> None:
    with pytest.raises(rid.DockerRunError, match="printed no JSON line"):
        rid._pair_reply("Traceback ...\nnot json\n", tmp_path)
    reply = FakeHost()._reply()
    with pytest.raises(rid.DockerRunError, match="wrote no"):
        rid._pair_reply(json.dumps(reply), tmp_path)


@pytest.mark.unit
def test_a_failed_preparer_stops_the_browser_and_names_its_output(
    host: FakeHost, tmp_path: Path
) -> None:
    real = host._docker

    def failing(argv: list[str]) -> Any:
        if argv[1] == "exec" and "prepare" in argv:
            return subprocess.CompletedProcess(argv, 1, "FAILED: no such story", "")
        return real(argv)

    host._docker = failing  # type: ignore[method-assign]
    with pytest.raises(rid.DockerRunError, match="FAILED: no such story"):
        _pair(host, tmp_path)
    assert len(host.docker_calls("stop")) == 1


@pytest.mark.unit
def test_pair_keychain_says_what_loads_without_bare(
    host: FakeHost, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _pair(host, tmp_path, "--pair-keychain")
    printed = capsys.readouterr().out
    assert "--bare" not in _printed_command(printed, "cd ")
    assert "CLAUDE.md, hooks and plugins do" in printed


@pytest.mark.unit
def test_pair_needs_only_the_personas(host: FakeHost, tmp_path: Path) -> None:
    environ = _environ()
    del environ["UX_TEST_AWS_ACCOUNT_ID"]
    assert _pair(host, tmp_path, environ=environ)[0] == 0
    del environ["UX_TEST_PERSONAS"]
    with pytest.raises(rid.DockerRunError, match="UX_TEST_PERSONAS"):
        _pair(host, tmp_path, environ=environ)


@pytest.mark.unit
def test_export_tree_refuses_a_ref_git_cannot_archive_and_an_escaping_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rid, "resolve_tool", lambda name: f"/fake/bin/{name}")
    with pytest.raises(rid.DockerRunError, match="40-hex"):
        rid.export_tree(tmp_path, "HEAD", ("scripts",), tmp_path / "x")

    def missing(argv: list[str], **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(
            argv, 128, b"", b"fatal: not a valid object name"
        )

    with pytest.raises(rid.DockerRunError, match="not a valid object name"):
        rid.export_tree(tmp_path, HEAD, ("scripts",), tmp_path / "x", run=missing)

    def escaping(argv: list[str], **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(argv, 0, _tar({"../outside.txt": b"x"}), b"")

    with pytest.raises(rid.DockerRunError, match="was refused"):
        rid.export_tree(tmp_path, HEAD, ("scripts",), tmp_path / "x", run=escaping)
    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.unit
def test_replay_against_another_stack_says_so(
    host: FakeHost, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "ci-run"
    source.mkdir()
    (source / rid.STORY_RUN_FILE).write_text(json.dumps(_run_record()))
    _stories(host, "--replay", str(source), "--stack", "my-stack")
    assert "WARNING: the run was against idp-ux-nightly" in capsys.readouterr().out
    (review,) = host.review_containers()
    assert _after(review.argv, "--stack") == "my-stack"
    (source / rid.STORY_RUN_FILE).write_text("[]")
    with pytest.raises(rid.DockerRunError, match="not a JSON object"):
        _stories(host, "--replay", str(source))


@pytest.mark.unit
def test_a_catalogue_that_fails_to_list_skips_the_nudge(host: FakeHost) -> None:
    def failing(argv: list[str]) -> Any:
        return subprocess.CompletedProcess(argv, 1)

    host._python = failing  # type: ignore[method-assign]
    assert _stories(host, "--list") == 1
    assert len(host.calls) == 1


@pytest.mark.unit
def test_the_terminal_check_reads_both_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    class Stream:
        def __init__(self, tty: bool) -> None:
            self.tty = tty

        def isatty(self) -> bool:
            return self.tty

    monkeypatch.setattr(rid.sys, "stdin", Stream(True))
    monkeypatch.setattr(rid.sys, "stdout", Stream(False))
    assert rid._is_terminal() is False
    monkeypatch.setattr(rid.sys, "stdout", Stream(True))
    assert rid._is_terminal() is True
