# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Everything the UX session needs to know about one merge request, as one record.

The record is collected from git, with title, author and URL taken from the API
when the caller has them, and it is JSON-serialisable on purpose: the place that
can collect it is not always the place that runs the session.

* In CI, ``ai_ux_test`` collects inline. Its precheck writes the record to
  ``ux-tests/inputs.json`` and the full run reads it back, so both halves of the
  job describe the same head even if the MR is pushed in between.
* The Docker review mode collects **on the host**, where git and the user's SSH
  agent work, and hands the file to a container that mounts the repository
  read-only and has neither.

Because the file can arrive from another process or another machine,
:meth:`MrInputs.from_json` validates its shape rather than trusting it: full
40-character SHAs, an integer iid, a well-formed changed-file list, and a
bounded title. The title is the MR author's text, and so are the branch names,
file names and diff; the prompt treats them as data, and nothing here
interprets them.

Refs are fetched into ``refs/ux-test/``, never ``refs/ai-review/``, so this job
and the code review cannot move a ref the other is reading. The diff is limited
to ``src/ui`` and cut on a line boundary by the same rule ``ai_mr_review`` uses,
and it is produced with ``--no-ext-diff --no-textconv``: the MR's
``.gitattributes`` can name a diff driver, and on a developer's host that driver
could be a program from their own git config. It is also read only up to that
budget (``ui_change.git_prefix``): what git prints past it is counted for
``ui_diff_bytes`` and dropped as it arrives, so a diff of any size costs the
precheck, which holds the review token and the personas, the budget and no more.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ux_runner import ui_change

#: The diff budget the prompt carries, the same order as ai_mr_review's.
DEFAULT_MAX_DIFF_BYTES = 120_000
REF_NAMESPACE = ui_change.REF_NAMESPACE

#: An MR title is the author's text; this bounds what reaches the prompt.
TITLE_CHARS = 300

_SHA_RE = re.compile(r"[0-9a-f]{40}")


class InputsError(Exception):
    """The MR's inputs could not be collected, or a record is malformed."""


@dataclass(frozen=True)
class MrInputs:
    """One MR as the UX session sees it (D-INPUTS)."""

    iid: int
    title: str
    author: str
    web_url: str
    source_branch: str
    target_branch: str
    head_sha: str
    base_sha: str
    changed: tuple[ui_change.ChangedFile, ...]
    ui_diff: str
    ui_diff_truncated: bool
    ui_diff_bytes: int
    title_from_git: bool

    def to_json(self) -> dict[str, Any]:
        """The ``inputs.json`` record."""
        return {
            "version": 1,
            "iid": self.iid,
            "title": self.title,
            "author": self.author,
            "web_url": self.web_url,
            "source_branch": self.source_branch,
            "target_branch": self.target_branch,
            "head_sha": self.head_sha,
            "base_sha": self.base_sha,
            "changed": [
                {"status": c.status, "path": c.path, "old_path": c.old_path}
                for c in self.changed
            ],
            "ui_diff": self.ui_diff,
            "ui_diff_truncated": self.ui_diff_truncated,
            "ui_diff_bytes": self.ui_diff_bytes,
            "title_from_git": self.title_from_git,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> MrInputs:
        """Rebuild a record, refusing any shape :meth:`to_json` would not write."""
        if not isinstance(data, Mapping):
            raise InputsError("inputs record is not a JSON object")
        if data.get("version") != 1:
            raise InputsError("inputs record has an unknown version")
        iid = data.get("iid")
        if isinstance(iid, bool) or not isinstance(iid, int) or iid < 1:
            raise InputsError("inputs record: iid must be a positive integer")
        for key in ("head_sha", "base_sha"):
            value = data.get(key)
            if not isinstance(value, str) or not _SHA_RE.fullmatch(value):
                raise InputsError(f"inputs record: {key} must be a full 40-hex SHA")
        for key in ("title", "author", "web_url", "source_branch", "target_branch"):
            if not isinstance(data.get(key), str):
                raise InputsError(f"inputs record: {key} must be text")
        if not isinstance(data.get("ui_diff"), str):
            raise InputsError("inputs record: ui_diff must be text")
        for key in ("ui_diff_truncated", "title_from_git"):
            if not isinstance(data.get(key), bool):
                raise InputsError(f"inputs record: {key} must be true or false")
        size = data.get("ui_diff_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise InputsError("inputs record: ui_diff_bytes must be a count")
        return cls(
            iid=iid,
            title=data["title"][:TITLE_CHARS],
            author=data["author"],
            web_url=data["web_url"],
            source_branch=data["source_branch"],
            target_branch=data["target_branch"],
            head_sha=data["head_sha"],
            base_sha=data["base_sha"],
            changed=_changed_from_json(data.get("changed")),
            ui_diff=data["ui_diff"],
            ui_diff_truncated=data["ui_diff_truncated"],
            ui_diff_bytes=size,
            title_from_git=data["title_from_git"],
        )


def _changed_from_json(value: object) -> tuple[ui_change.ChangedFile, ...]:
    if not isinstance(value, list):
        raise InputsError("inputs record: changed must be a list")
    entries: list[ui_change.ChangedFile] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"status", "path", "old_path"}:
            raise InputsError(
                "inputs record: each changed entry needs status, path and old_path"
            )
        status, path, old_path = item["status"], item["path"], item["old_path"]
        if not isinstance(status, str) or not status:
            raise InputsError("inputs record: a changed entry has no status")
        if not isinstance(path, str) or not path:
            raise InputsError("inputs record: a changed entry has no path")
        if old_path is not None and not isinstance(old_path, str):
            raise InputsError("inputs record: old_path must be text or null")
        entries.append(ui_change.ChangedFile(status, path, old_path))
    return tuple(entries)


def cut_diff(diff: str, max_bytes: int, *, partial: bool = False) -> tuple[str, bool]:
    """``diff`` within ``max_bytes`` of UTF-8, cut on a line boundary.

    The rule is ``ai_mr_review.build_diff``'s, so the tail is never a half-written
    hunk header, and the second value says whether anything was cut. ``partial``
    says ``diff`` is only the start of what git printed, so its last line is cut
    even when the rest fits: text mode turns ``\\r\\n`` into ``\\n``, so the start
    of a longer diff can decode to fewer bytes than were read.
    """
    encoded = diff.encode()
    if len(encoded) <= max_bytes and not partial:
        return diff, False
    return encoded[:max_bytes].decode(errors="ignore").rsplit("\n", 1)[0], True


def _commit_of(repo_dir: Path, ref: str, git: Callable[..., str]) -> str:
    try:
        sha = git(repo_dir, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    except ui_change.UiChangeError as error:
        raise InputsError(f"cannot resolve {ref}: {error}") from error
    if not _SHA_RE.fullmatch(sha):
        raise InputsError(f"{ref} did not resolve to a commit")
    return sha


def collect(
    repo_dir: Path,
    iid: int,
    target_branch: str,
    *,
    remote: str = "origin",
    max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES,
    title: str | None = None,
    author: str | None = None,
    web_url: str = "",
    source_branch: str = "",
    fetch: bool = True,
    git: Callable[..., str] = ui_change.git,
    git_prefix: Callable[..., tuple[str, int]] = ui_change.git_prefix,
) -> MrInputs:
    """Collect the MR's head, merge base, changed files and ``src/ui`` diff.

    With ``fetch``, the MR head and the target branch are fetched into
    :data:`REF_NAMESPACE` first; without it those refs must already exist. A
    ``title`` of None means no API metadata was available, so the head commit's
    subject and author stand in and ``title_from_git`` records that. The diff is
    read through ``git_prefix`` up to ``max_diff_bytes``, and ``ui_diff_bytes``
    is the count of what git printed, read or not.
    """
    if isinstance(iid, bool) or not isinstance(iid, int) or iid < 1:
        raise InputsError("the MR iid must be a positive integer")
    try:
        target = ui_change.check_branch(target_branch)
        head_ref = f"{REF_NAMESPACE}/{iid}"
        base_ref = ui_change.target_ref(target)
        if fetch:
            for refspec in (
                f"+refs/merge-requests/{iid}/head:{head_ref}",
                f"+refs/heads/{target}:{base_ref}",
            ):
                git(repo_dir, "fetch", "--quiet", remote, refspec)
        head = _commit_of(repo_dir, head_ref, git)
        base = ui_change.merge_base(repo_dir, base_ref, head, run=git)
        changed = ui_change.changed_files(repo_dir, base, head, run=git)
        diff, diff_bytes = git_prefix(
            repo_dir,
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames",
            f"{base}..{head}",
            "--",
            ui_change.UI_PREFIX.rstrip("/"),
            keep=max_diff_bytes,
        )
        subject = git_author = ""
        if title is None:
            line = git(repo_dir, "log", "-1", "--format=%s%x00%an", head)
            subject, _, git_author = line.rstrip("\n").partition("\0")
    except ui_change.UiChangeError as error:
        raise InputsError(str(error)) from error
    ui_diff, truncated = cut_diff(
        diff, max_diff_bytes, partial=diff_bytes > max_diff_bytes
    )
    return MrInputs(
        iid=iid,
        title=(subject if title is None else title)[:TITLE_CHARS],
        author=author if author is not None else (git_author or "unknown"),
        web_url=web_url,
        source_branch=source_branch,
        target_branch=target,
        head_sha=head,
        base_sha=base,
        changed=tuple(changed),
        ui_diff=ui_diff,
        ui_diff_truncated=truncated,
        ui_diff_bytes=diff_bytes,
        title_from_git=title is None,
    )


def write(inputs: MrInputs, path: Path) -> Path:
    """Write ``inputs`` as JSON to ``path`` and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(inputs.to_json(), indent=2, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def read(path: Path) -> MrInputs:
    """Read and validate a record written by :func:`write`."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise InputsError(f"cannot read {path}: {error}") from error
    return MrInputs.from_json(data)
