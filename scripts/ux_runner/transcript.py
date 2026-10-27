# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""What a watched story session's Claude Code transcript says: who typed, what it used.

A watched flow runs the pinned Claude TUI, which has no ``--output-format json`` to
read a result from, so what the run needs about the session comes from the session
file Claude Code writes, ``$HOME/.claude/projects/<dir>/<session id>.jsonl``, one
JSON object per line. That format is Claude Code's own and unversioned, so this
module reads only two things from it, and pins that reading to sessions recorded
at the pinned release, offline, in ``scripts/tests/fixtures/ux_story_transcript/``:

* **The person's messages.** A ``user`` line that is not meta, not a sidechain and
  not a tool result, whose message is text: a string, or text blocks and no tool
  result. At 2.1.281 such a line carries ``origin.kind`` ``human`` and
  ``promptSource`` ``typed`` whether it was the opening message or typed later,
  so the opening message is one of them and a session with two or more is
  ``assisted``. A line naming another origin is not the person's, a slash command
  that only ends the session (``/exit``, ``/quit``) is not a message, and a local
  command's recorded output is not one either. Nothing depends on line order,
  because the file's is not the conversation's: at 2.1.281 a tool result can come
  before the message that led to it.
* **What it used.** The four priced token counts of every assistant message,
  counted once by its message id (the largest value of each count when one
  message spans lines), summed: what ``pinned_cost`` prices. Beside them, the last
  ``cost-state`` line's ``totalCostUSD`` and per-model counts, the CLI's own
  figures, which include its side requests (the session title) and which only a
  session that ended through ``/exit`` writes.

Every string in a transcript is page or model text from a session that browsed a
page under test. None leaves this module: it returns counts, and the model ids and
the CLI version only when each matches a fixed pattern.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TRANSCRIPT_MAX_BYTES = 64 * 1024 * 1024
USAGE_KEYS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
COST_STATE_KEYS: dict[str, str] = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheReadInputTokens": "cache_read_input_tokens",
    "cacheCreationInputTokens": "cache_creation_input_tokens",
}
ENDING_COMMANDS: tuple[str, ...] = ("/exit", "/quit")
COMMAND_OUTPUT_TAGS: tuple[str, ...] = (
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
)
SYNTHETIC_MODEL = "<synthetic>"
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,119}")
VERSION_RE = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}")
COMMAND_RE = re.compile(r"\A<command-name>\s*(/[A-Za-z0-9:_-]+)\s*</command-name>")
COUNT_MAX = 10**12
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_BINARY", 0)
)


class TranscriptError(Exception):
    """A transcript file that cannot be read; the message names why, never its text."""


@dataclass(frozen=True)
class Transcript:
    """What one session file says, as counts.

    ``found`` is False when no session file could be read, which leaves whether
    anyone typed unknown rather than no. ``user_messages`` counts the person's
    messages, the opening one included. ``usage`` holds the four priced counts
    summed over distinct assistant messages, or None when none reported one.
    """

    found: bool
    user_messages: int
    usage: dict[str, int] | None
    tool_results: int = 0
    assistant_messages: int = 0
    models: tuple[str, ...] = ()
    version: str | None = None
    cli_cost_usd: float | None = None
    model_usage: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    lines: int = 0
    unreadable_lines: int = 0

    @property
    def assisted(self) -> bool:
        """Whether a message reached the session after the opening one."""
        return self.user_messages > 1

    def to_json(self) -> dict[str, Any]:
        """The record a watched run keeps of its session: counts and checked ids."""
        return {
            "found": self.found,
            "assisted": self.assisted,
            "user_messages": self.user_messages,
            "tool_results": self.tool_results,
            "assistant_messages": self.assistant_messages,
            "usage": None if self.usage is None else dict(self.usage),
            "models": list(self.models),
            "version": self.version,
            "cli_cost_usd": self.cli_cost_usd,
            "model_usage": {
                model: dict(counts) for model, counts in self.model_usage.items()
            },
            "lines": self.lines,
            "unreadable_lines": self.unreadable_lines,
        }


NOT_FOUND = Transcript(found=False, user_messages=0, usage=None)


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= COUNT_MAX else None


def _priced(usage: object) -> dict[str, int] | None:
    """The priced counts a message's ``usage`` reports as whole numbers, or None."""
    if not isinstance(usage, Mapping):
        return None
    counts = {key: _count(usage.get(key)) for key in USAGE_KEYS}
    kept = {key: value for key, value in counts.items() if value is not None}
    return kept or None


def _text(content: object) -> str | None:
    """A user message's text, or None when it is not text alone."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    kinds = {block.get("type") for block in content if isinstance(block, Mapping)}
    if "tool_result" in kinds or "text" not in kinds:
        return None
    return " ".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
    )


def _is_tool_result(content: object) -> bool:
    return isinstance(content, list) and any(
        isinstance(block, Mapping) and block.get("type") == "tool_result"
        for block in content
    )


def is_person_message(entry: Mapping[str, Any]) -> bool:
    """Whether one transcript line is a message the person sent (rules above)."""
    if entry.get("type") != "user":
        return False
    if any(entry.get(flag) is True for flag in ("isMeta", "isSidechain")):
        return False
    if entry.get("isCompactSummary") is True or "toolUseResult" in entry:
        return False
    origin = entry.get("origin")
    if isinstance(origin, Mapping) and origin.get("kind") not in (None, "human"):
        return False
    message = entry.get("message")
    if not isinstance(message, Mapping) or message.get("role") != "user":
        return False
    text = _text(message.get("content"))
    if text is None or not text.strip():
        return False
    stripped = text.strip()
    if stripped.startswith(COMMAND_OUTPUT_TAGS):
        return False
    command = COMMAND_RE.match(stripped)
    named = command.group(1) if command else stripped
    return named not in ENDING_COMMANDS


def _cost_state(
    entry: Mapping[str, Any],
) -> tuple[float | None, dict[str, dict[str, int]]]:
    total = entry.get("totalCostUSD")
    cost = (
        float(total)
        if isinstance(total, (int, float))
        and not isinstance(total, bool)
        and math.isfinite(total)
        and total >= 0
        else None
    )
    models: dict[str, dict[str, int]] = {}
    usage = entry.get("modelUsage")
    for model, counts in usage.items() if isinstance(usage, Mapping) else ():
        if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
            continue
        if not isinstance(counts, Mapping):
            continue
        kept = {
            ours: number
            for theirs, ours in COST_STATE_KEYS.items()
            if (number := _count(counts.get(theirs))) is not None
        }
        if kept:
            models[model] = kept
    return cost, models


def parse_transcript(text: str) -> Transcript:
    """One session file's text read as a :class:`Transcript`.

    A line that is not a JSON object is counted in ``unreadable_lines`` and
    skipped, and a field of an unexpected type counts as absent, so a format
    change shows as missing counts rather than as an exception.
    """
    lines = unreadable = person = results = 0
    per_message: dict[str, dict[str, int]] = {}
    models: list[str] = []
    version: str | None = None
    cost: float | None = None
    model_usage: dict[str, dict[str, int]] = {}
    for number, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        lines += 1
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            unreadable += 1
            continue
        if not isinstance(entry, Mapping):
            unreadable += 1
            continue
        stated = entry.get("version")
        if version is None and isinstance(stated, str) and VERSION_RE.fullmatch(stated):
            version = stated
        kind = entry.get("type")
        if kind == "cost-state":
            cost, model_usage = _cost_state(entry)
            continue
        message = entry.get("message")
        if not isinstance(message, Mapping) or entry.get("isSidechain") is True:
            continue
        if kind == "user":
            if is_person_message(entry):
                person += 1
            elif _is_tool_result(message.get("content")):
                results += 1
        elif kind == "assistant":
            model = message.get("model")
            if (
                isinstance(model, str)
                and model != SYNTHETIC_MODEL
                and MODEL_RE.fullmatch(model)
                and model not in models
            ):
                models.append(model)
            ident = message.get("id")
            key = ident if isinstance(ident, str) and ident else f"line {number}"
            counts = _priced(message.get("usage"))
            if counts is not None:
                kept = per_message.setdefault(key, {})
                for name, value in counts.items():
                    kept[name] = max(kept.get(name, 0), value)
            else:
                per_message.setdefault(key, {})
    total: dict[str, int] = {}
    for counts in per_message.values():
        for name, value in counts.items():
            total[name] = total.get(name, 0) + value
    return Transcript(
        found=True,
        user_messages=person,
        usage=total or None,
        tool_results=results,
        assistant_messages=len(per_message),
        models=tuple(models),
        version=version,
        cli_cost_usd=cost,
        model_usage=model_usage,
        lines=lines,
        unreadable_lines=unreadable,
    )


def read_transcript_file(
    path: Path, *, limit: int = TRANSCRIPT_MAX_BYTES
) -> Transcript:
    """One session file, opened without following a link and read to ``limit`` bytes.

    :class:`TranscriptError` for anything but a regular file of at most ``limit``
    bytes, because a cut transcript could hide a message the person typed.
    """
    try:
        descriptor = os.open(path, _READ_FLAGS)
    except OSError as error:
        raise TranscriptError(
            f"the session file cannot be opened ({error.strerror or type(error).__name__})"
        ) from None
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise TranscriptError("the session file is not a regular file")
    with os.fdopen(descriptor, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise TranscriptError(f"the session file is larger than {limit} bytes")
    return parse_transcript(data.decode("utf-8", errors="replace"))


def session_files(home: Path) -> list[Path]:
    """The session files under ``home/.claude/projects``, newest first.

    Only regular files, never a link, one directory level down, as Claude Code
    lays them out.
    """
    projects = home / ".claude" / "projects"
    found: list[tuple[float, str, Path]] = []
    try:
        folders = sorted(projects.iterdir())
    except OSError:
        return []
    for folder in folders:
        try:
            if folder.is_symlink() or not folder.is_dir():
                continue
            for path in folder.iterdir():
                status = os.lstat(path)
                if path.suffix == ".jsonl" and stat.S_ISREG(status.st_mode):
                    found.append((status.st_mtime, str(path), path))
        except OSError:
            continue
    return [path for _, _, path in sorted(found, reverse=True)]


def read_transcript(home: Path, *, limit: int = TRANSCRIPT_MAX_BYTES) -> Transcript:
    """The newest session under ``home``, or :data:`NOT_FOUND` when none can be read.

    A watched run's ``HOME`` is private to its one session, so the newest file is
    that session's. This never raises: a file that cannot be read gives
    :data:`NOT_FOUND`, whose ``found`` says the session's assistance is unknown.
    """
    for path in session_files(home):
        try:
            return read_transcript_file(path, limit=limit)
        except TranscriptError:
            return NOT_FOUND
    return NOT_FOUND
