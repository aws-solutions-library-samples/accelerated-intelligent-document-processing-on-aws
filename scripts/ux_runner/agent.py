# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Build and run the UX session's ``claude``: environment, MCP config, argv, result.

This is the unattended counterpart of an interactive ux-test session, and it is
built around the same property ``scripts/sdlc/ai_mr_review.py`` is: **the model
never holds anything it could misuse.** The pages it reads are rendered by the
MR's own code, so whatever a page talks it into wanting, it must have no means to
do. Four pieces carry that, one function each.

The environment is an allowlist
-------------------------------
:func:`child_env` starts from nothing and copies only :data:`CHILD_ENV_ALLOWLIST`
from the job, then adds fixed settings and the AWS session it is handed.
``ai_mr_review`` strips a named list of secrets instead, which is right for a job
that holds one token; this job also holds ``GITLAB_REVIEW_TOKEN``, the persona
passwords in ``UX_TEST_PERSONAS``, the stack identifiers in ``UX_TEST_UI_ENV`` and
its own AWS credential, and a denylist passes every variable nobody thought to name.
The AWS session ``claude`` receives is the Bedrock-scoped one from
``account.scoped_credentials`` when the job's role can re-assume itself, and the
job's own credentials when it cannot, which the orchestrator says in a
``WARNING:`` line before the session and records as ``credential_scope.claude:
ambient`` (on a developer's SSO session that is the usual case). Only the three
AWS variables are accepted for it.
``AWS_EC2_METADATA_DISABLED`` is set, so a session handed no key material fails
rather than falling through to instance metadata, which would run the model on a
credential nobody chose while the summary said ``ambient``.

The allowlist decides what ``claude`` inherits, not what it could read. ``claude``
and both MCP servers run as the job's own uid, the orchestrator's, whose
``/proc/<pid>/environ`` still holds ``GITLAB_REVIEW_TOKEN``, ``UX_TEST_PERSONAS``,
``UX_TEST_UI_ENV`` and the job's own AWS keys. What keeps those from the model is
that it holds no tool that reads a file: ``--tools ''`` leaves no built-in,
``upload_file`` is denied, and the browser's policy blocks ``file://``. Render,
which has no such tool sandbox in front of it, runs as a uid of its own instead
(``recording.render_video``).

Every MCP server starts under ``env -i``
----------------------------------------
Claude Code starts a stdio MCP server with an environment derived from its own,
and its own holds the Bedrock session. Rather than depend on which variables that
derivation passes, :func:`mcp_servers` runs both servers through
``/usr/bin/env -i`` with an explicit ``PATH``, ``HOME`` and locale, so
chrome-devtools-mcp and the recorder adapter receive no credential at all.

The argv is fixed and carries no payload
----------------------------------------
:func:`claude_argv` names the permission mode, removes the built-in tools with
``--tools ''``, loads only the MCP config written here (``--strict-mcp-config``),
skips hooks, plugins, auto-memory and ``CLAUDE.md`` discovery (``--bare``), writes
no session transcript (``--no-session-persistence``) and caps turns and spend. The
model is chosen by ``ANTHROPIC_MODEL`` in the environment, and the prompt goes on
stdin (:func:`run_agent`), never in argv: see ``processes``.

The result is parsed, not trusted
---------------------------------
:func:`parse_output` reads Claude Code's ``--output-format json`` object into an
:class:`AgentResult` whose ``stop`` says why the session ended. The report text is
model output from a session that browsed attacker-influenced pages; everything
downstream treats it as untrusted.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import paths, processes, tool_profiles

#: Named on the command line because it is what makes the tool lists mean
#: anything: ``manual`` means "ask", and under ``-p`` there is nobody to ask, so
#: anything outside the granted set is refused. A user-level settings file that
#: sets another default mode is overridden by naming this one.
PERMISSION_MODE = "manual"

#: The only variables copied from the job's environment into ``claude``'s.
#: Everything else it receives is set by :func:`child_env` itself.
CHILD_ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "TZ")

#: How chrome-devtools-mcp is started. It attaches to the harness's Chrome on
#: loopback rather than launching one of its own, redacts sensitive headers in
#: what it returns, sends no usage statistics or CrUX lookups and loads no source
#: maps; and the emulation, performance and memory categories are switched off,
#: so those tools are not served at all. ``--javascriptEvaluation=false`` is what
#: keeps page script out of the granted tools: it defaults to true, and then
#: ``navigate_page`` takes an ``initScript`` to run on the next document and
#: follows ``javascript:`` and ``data:`` URLs, none of which denying
#: ``evaluate_script`` by name reaches. ``--no-page-id-routing`` makes every page
#: tool act on the one page the harness opened; routing defaults to on, and then
#: each of them requires a ``pageId`` that only the denied ``list_pages`` reports.
#: Every flag was checked against 1.10.1, whose option parser warns on an unknown
#: one, and against its tools/list. The ``filePath`` that ``take_screenshot`` and
#: ``take_snapshot`` take may name a file under ``--filesystemRoot``, which
#: :func:`mcp_servers` adds, or under any root the MCP client reports, since 1.10.1
#: writes under the union of the two; a client reports its working directory, so
#: ``claude``'s is a write root as well, and :func:`run_agent` refuses one that is
#: not a private, empty directory.
DEVTOOLS_FLAGS: tuple[str, ...] = (
    "--browserUrl",
    "http://127.0.0.1:9222",
    "--redactNetworkHeaders",
    "--no-usage-statistics",
    "--no-performance-crux",
    "--no-source-maps",
    "--categoryEmulation=false",
    "--categoryPerformance=false",
    "--categoryMemory=false",
    "--javascriptEvaluation=false",
    "--no-page-id-routing",
)

#: ``PATH`` for the MCP servers when the caller does not supply one. The
#: orchestrator prepends the directory holding ``node``.
DEFAULT_SEARCH_PATH = "/usr/local/bin:/usr/bin:/bin"

#: The only names the AWS session handed to :func:`child_env` may use.
_AWS_ENV_NAMES = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


class AgentError(Exception):
    """Raised when the session cannot be configured or its output cannot be read."""


@dataclass(frozen=True)
class AgentResult:
    """The parsed outcome of one ``claude -p`` session.

    ``stop`` is one of ``end_turn``, ``max_turns``, ``budget`` or ``error``.
    """

    text: str
    cost_usd: float | None
    num_turns: int | None
    duration_s: float | None
    stop: str
    is_error: bool


def aws_env_from(source: Mapping[str, str]) -> dict[str, str]:
    """The AWS key material in ``source``, for the ambient-credential fallback.

    Copies ``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY`` and
    ``AWS_SESSION_TOKEN`` when they are present and non-empty. An empty session
    token handed on would be sent as an empty security token header and rejected,
    where an absent one is simply not sent.
    """
    return {name: source[name] for name in _AWS_ENV_NAMES if source.get(name)}


def child_env(
    *,
    home: Path,
    tmpdir: Path,
    model: str,
    region: str,
    aws: Mapping[str, str],
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The complete environment ``claude`` runs in.

    :data:`CHILD_ENV_ALLOWLIST` names from ``source`` (``os.environ`` by default),
    then the fixed settings: a private ``HOME`` and ``TMPDIR``, Bedrock on, the
    model and region, instance metadata off, ``CI=true`` so nothing waits for a
    person, and every auto-update, telemetry, error-report and non-essential
    request off. Then the
    ``aws`` mapping, which may only use the three AWS key names; anything else in
    it raises :class:`AgentError` naming the keys, never their values. Nothing else
    from ``source`` passes: no token, no ``UX_TEST_*``, no proxy variable and no
    ``AWS_PROFILE``.
    """
    parent = os.environ if source is None else source
    foreign = sorted(set(aws) - set(_AWS_ENV_NAMES))
    if foreign:
        raise AgentError(
            "only AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and AWS_SESSION_TOKEN "
            f"may carry the session for claude; refused: {', '.join(foreign)}"
        )
    env = {name: parent[name] for name in CHILD_ENV_ALLOWLIST if name in parent}
    env.update(
        {
            "HOME": str(home),
            "TMPDIR": str(tmpdir),
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_MODEL": model,
            "AWS_REGION": region,
            "AWS_DEFAULT_REGION": region,
            "AWS_EC2_METADATA_DISABLED": "true",
            "CI": "true",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
    )
    env.update(aws)
    return env


def mcp_servers(
    *,
    toolchain: paths.Toolchain,
    recorder_adapter: Path,
    recorder_out: Path,
    python: str,
    home: Path,
    fs_root: Path,
    search_path: str = DEFAULT_SEARCH_PATH,
) -> dict[str, Any]:
    """The two MCP server definitions, keyed by the names the tool ids use.

    Both are ``/usr/bin/env -i`` with an explicit environment, so neither inherits
    ``claude``'s, and both have an empty ``env`` map for the same reason.
    chrome-devtools-mcp writes files only under ``fs_root`` and the roots the MCP
    client reports, which is why :func:`run_agent` holds ``claude``'s working
    directory to the same standard as ``fs_root``, and it is told not to check
    for updates. The recorder adapter runs under ``python -I``,
    which ignores ``PYTHON*`` variables and the user site directory, so it is
    kept from writing bytecode into the pinned harness by ``-B`` on its command
    line, where ``PYTHONDONTWRITEBYTECODE`` would be ignored; it writes only under
    ``recorder_out``.
    """
    base = ["-i", f"PATH={search_path}", f"HOME={home}", "LANG=C.UTF-8"]
    return {
        tool_profiles.CHROME_DEVTOOLS_SERVER: {
            "type": "stdio",
            "command": "/usr/bin/env",
            "args": [
                *base,
                "CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS=1",
                str(toolchain.devtools_mcp),
                *DEVTOOLS_FLAGS,
                f"--filesystemRoot={fs_root}",
            ],
            "env": {},
        },
        tool_profiles.RECORDER_SERVER: {
            "type": "stdio",
            "command": "/usr/bin/env",
            "args": [
                *base,
                python,
                "-I",
                "-B",
                str(recorder_adapter),
                "--out",
                str(recorder_out),
            ],
            "env": {},
        },
    }


def write_mcp_config(path: Path, servers: Mapping[str, Any]) -> Path:
    """Write ``{"mcpServers": servers}`` to ``path``, readable by its owner only.

    The file is created with mode 0600 and set to 0600 again before anything is
    written, so a pre-existing file with a wider mode is narrowed rather than
    trusted, and there is no moment at which it is readable by anyone else. A
    symlink at ``path`` is refused rather than followed.
    """
    payload = json.dumps({"mcpServers": dict(servers)}, indent=2) + "\n"
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(payload)
    return path


def claude_argv(
    claude: Path,
    mcp_config: Path,
    *,
    max_turns: int,
    max_budget_usd: float,
    profile: tool_profiles.ToolProfile = tool_profiles.EXPERT,
) -> list[str]:
    """The exact command line of the UX session. The prompt is not part of it.

    ``profile`` supplies the granted and the denied tools. The default,
    ``tool_profiles.EXPERT``, is the MR UX test's own pair of lists.
    """
    return [
        str(claude),
        "-p",
        "--bare",
        "--output-format",
        "json",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp_config),
        "--permission-mode",
        PERMISSION_MODE,
        "--tools",
        "",
        "--allowedTools",
        *profile.allowed,
        "--disallowedTools",
        *profile.disallowed,
        "--max-turns",
        str(max_turns),
        "--max-budget-usd",
        f"{max_budget_usd:.2f}",
    ]


def _number(value: object) -> float | None:
    """``value`` as a float when it is a JSON number, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _count(value: object) -> int | None:
    """``value`` when it is a JSON integer, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _result_object(payload: object) -> dict[str, Any] | None:
    """The result message of a ``--output-format json`` answer.

    A single object is the documented shape. A list of messages, which some
    releases print, is searched from the end for the one whose ``type`` is
    ``result``.
    """
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        for item in reversed(payload):
            if isinstance(item, dict) and item.get("type") == "result":
                return item
    return None


def parse_output(stdout: str) -> AgentResult:
    """Read Claude Code's JSON result into an :class:`AgentResult`.

    ``subtype`` decides ``stop``: ``success`` is ``end_turn``, ``error_max_turns``
    is ``max_turns``, any subtype naming a budget is ``budget``, and anything else
    is ``error``. Cost and turn count are kept only when they are numbers, so a
    missing or renamed field reads as unknown rather than as zero. Output that is
    not JSON raises :class:`AgentError` showing its first 400 characters.
    """
    try:
        payload = json.loads(stdout)
    except ValueError as error:
        raise AgentError(f"claude did not return JSON: {stdout[:400]!r}") from error
    result = _result_object(payload)
    if result is None:
        raise AgentError(f"claude did not return a result object: {stdout[:400]!r}")
    subtype = str(result.get("subtype") or "")
    if subtype == "success":
        stop = "end_turn"
    elif subtype == "error_max_turns":
        stop = "max_turns"
    elif "budget" in subtype:
        stop = "budget"
    else:
        stop = "error"
    duration_ms = _number(result.get("duration_ms"))
    return AgentResult(
        text=str(result.get("result") or "").strip(),
        cost_usd=_number(result.get("total_cost_usd")),
        num_turns=_count(result.get("num_turns")),
        duration_s=duration_ms / 1000 if duration_ms is not None else None,
        stop=stop,
        is_error=bool(result.get("is_error")),
    )


def _require_private_empty_dir(path: Path) -> None:
    """Refuse ``path`` unless it is an empty ``0o700`` directory this user owns."""
    try:
        status = os.lstat(path)
        empty = stat.S_ISDIR(status.st_mode) and not any(path.iterdir())
    except OSError as error:
        raise AgentError(
            f"claude's working directory cannot be used: {type(error).__name__}"
        ) from None
    if not (
        empty and stat.S_IMODE(status.st_mode) == 0o700 and status.st_uid == os.getuid()
    ):
        raise AgentError(
            "claude's working directory must be an empty directory that only its "
            "owner, this user, can enter: chrome-devtools-mcp may write files there"
        )


def run_agent(
    prompt: str,
    *,
    argv: Sequence[str],
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
    runner: Callable[..., processes.Completed] = processes.run_in_group,
) -> tuple[AgentResult | None, processes.Completed]:
    """Run the session with ``prompt`` on stdin and return what it produced.

    ``(None, completed)`` when it timed out, or when it exited non-zero without a
    JSON answer: there is no result to read, and ``completed`` says why. A zero
    exit with unreadable output raises :class:`AgentError`, because that is the
    CLI misbehaving rather than the session failing, and so does an answer longer
    than ``runner`` keeps (``processes.MAX_OUTPUT_BYTES``), which is cut and
    cannot be read either.

    ``cwd`` must be an empty directory, not a symlink, that only its owner (this
    process's user) can enter, or :class:`AgentError` is raised before anything
    runs. The working directory is one of the roots chrome-devtools-mcp may write
    a screenshot or snapshot under, so pointing it at the harness, the artifacts
    or anything else that holds files would let the model write there.
    """
    _require_private_empty_dir(cwd)
    completed = runner(argv, input_text=prompt, env=env, cwd=cwd, timeout=timeout)
    if completed.timed_out:
        return None, completed
    if completed.stdout_dropped:
        raise AgentError(
            f"claude's answer was cut {completed.stdout_dropped} bytes short at the "
            "capture bound, so it cannot be read"
        )
    try:
        result = parse_output(completed.stdout)
    except AgentError:
        if completed.returncode != 0:
            return None, completed
        raise
    return result, completed
