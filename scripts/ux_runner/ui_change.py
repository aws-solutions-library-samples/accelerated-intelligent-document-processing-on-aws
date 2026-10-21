#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Classify a merge request's changed files for the MR UX test, from git alone.

Two jobs ask the same question of the same diff, "does this MR change anything a
person would see in the web UI?", and they must not be able to answer it
differently:

* ``ux_test_build`` runs this file **by path, on a bare python3**, before ``npm
  ci``. It writes ``ui-change.json`` and skips the build when only tests changed
  under ``src/ui/``, so a test-only MR costs seconds rather than an install.
* ``ai_ux_test`` recomputes the same classification with the **target branch's**
  copy of this module, the pinned harness, and never trusts the artifact.
  ``ux_test_build`` runs the MR's own code, so ``ui-change.json`` says whatever
  the MR wants it to say. Downstream it is diagnostics and nothing more.

That is why this file is self-contained standard library, with no ``ux_runner``
import and no ``yaml``, and why the classification is a pure function of
``git diff --name-status`` output: the same input gives the same answer in both
jobs, and :func:`classify` can be tested without a repository.

What counts as visible
----------------------
A UI file is any changed path under :data:`UI_PREFIX`; for a rename, either side
counts, so moving a component out of the tree is still a UI change. A UI file is
test-only when it matches :data:`TEST_PATH_GLOBS`, and for a rename **both**
sides must, because a test file renamed into a component is a new component. Every
other UI file is user-visible, deletions included: a screen that loses a
component has changed. The MR is worth a browser exactly when at least one
user-visible file exists.

:data:`API_CONTRACT_GLOBS` and :data:`BACKEND_GLOBS` are read over every changed
path, inside ``src/ui/`` or not. They do not decide anything here; they become
caveats in the note, because the review stack runs its own deployed backend
rather than the MR's.

Why the globs are matched here
------------------------------
Python 3.12 has no ``PurePath.full_match``, and :mod:`fnmatch` lets ``*`` cross
``/`` while giving ``**/`` no zero-segment form, so ``**/*.test.*`` would miss a
test file at the top of a directory listing and ``src/ui/src/*.tsx`` would match
at any depth. :func:`glob_to_regex` implements the three forms the patterns here
use and anchors both ends.

Reading git
-----------
The changed paths and the diff are the MR author's, and so is their size, while
the process asking holds the review token and the personas in ``ai_ux_test``'s
precheck. So no git command's output is read whole: :func:`git_prefix` keeps the
first bytes up to a bound and counts the rest as it arrives. ``mr_inputs`` reads
the ``src/ui`` diff that way, up to the prompt's budget, and :func:`git` refuses
any other command that prints more than :data:`MAX_OUTPUT_BYTES`.

Usage
-----
    # ux_test_build, in the MR checkout
    python3 scripts/ux_runner/ui_change.py --out ui-change.json

    # against local branches, without touching a remote
    python3 scripts/ux_runner/ui_change.py --out /tmp/ui-change.json --no-fetch
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

#: Where the web UI lives. A changed path under it is a UI file.
UI_PREFIX = "src/ui/"

#: A UI file matching one of these is a test. A change made only of these moves
#: no screen, so it needs no browser.
TEST_PATH_GLOBS = ("**/__tests__/**", "**/*.test.*", "**/*.spec.*")

#: Files that define the contract between the UI and its API. The review stack
#: runs its own deployed backend, not this MR's, so a screen that needs a contract
#: this MR changes is reported as blocked rather than broken.
API_CONTRACT_GLOBS = (
    "**/schema.graphql",
    "**/api_validation_spec.json",
    "**/api_rbac_manifest.json",
    "src/ui/src/graphql/operations/**",
)

#: Files whose change only takes effect once deployed. The review stack runs its
#: own deployment, not this MR's, so the note says when an MR carries any.
BACKEND_GLOBS = (
    "template.yaml",
    "nested/**",
    "patterns/**",
    "src/lambda/**",
    "lib/idp_common_pkg/idp_common/**",
    "config_library/**",
)

#: The local ref namespace both UX jobs fetch into. Distinct from ai_mr_review's
#: ``refs/ai-review``, so neither job can move a ref the other is reading.
REF_NAMESPACE = "refs/ux-test"

#: Variables :func:`git` takes out of git's environment, by prefix: the review
#: token, the personas and stack identifiers, and every AWS credential. git needs
#: none of them, and the UX job's precheck holds them all. ``CI_JOB_TOKEN`` stays,
#: because a runner's credential helper may read it for the fetch.
_CREDENTIAL_ENV_PREFIXES = ("AWS_", "GITLAB_", "UX_TEST_")

#: The most :func:`git` reads of what one git command prints. A SHA, a subject
#: line and the changed-file list of any real change are far smaller, so a command
#: that prints more is refused rather than read.
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
#: How much of git's stderr is kept, for the start of an error message.
_STDERR_BYTES = 64 * 1024
#: How much one read takes from a pipe.
_READ_BYTES = 64 * 1024
#: How long git's pipes get to close once git has been killed for overrunning.
_KILLED_PIPE_SECONDS = 5.0

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")


class UiChangeError(Exception):
    """git could not answer, so the classification cannot be trusted."""


@dataclass(frozen=True)
class ChangedFile:
    """One entry of ``git diff --name-status``; ``old_path`` only for R and C."""

    status: str
    path: str
    old_path: str | None = None


@dataclass(frozen=True)
class UiChange:
    """The changed paths, sorted into the buckets the UX test acts on."""

    ui_files: tuple[str, ...]
    user_visible: tuple[str, ...]
    test_only: tuple[str, ...]
    api_contract: tuple[str, ...]
    backend: tuple[str, ...]

    @property
    def skip(self) -> bool:
        """True when nothing a person could see changed."""
        return not self.ui_files or not self.user_visible

    @property
    def reason(self) -> str:
        """Why :attr:`skip` holds, in the words the job prints; empty otherwise."""
        if not self.ui_files:
            return "no change under src/ui"
        if not self.user_visible:
            return "only tests changed under src/ui"
        return ""

    def to_json(
        self, *, head_sha: str, base_sha: str, target_branch: str
    ) -> dict[str, Any]:
        """The ``ui-change.json`` record (D-UICHANGE)."""
        return {
            "version": 1,
            "head_sha": head_sha,
            "base_sha": base_sha,
            "target_branch": target_branch,
            "ui_files": list(self.ui_files),
            "user_visible": list(self.user_visible),
            "test_only": list(self.test_only),
            "api_contract": list(self.api_contract),
            "backend": list(self.backend),
            "skip": self.skip,
            "reason": self.reason,
        }


@functools.lru_cache(maxsize=256)
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile a path glob into a regex anchored at both ends.

    ``**/`` matches zero or more whole segments, a trailing ``/**`` matches
    everything below, ``*`` stays within one segment, ``?`` is one character
    other than ``/``, and everything else is literal.
    """
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:[^/]+/)*")
            index += 3
        elif pattern.startswith("/**", index) and index + 3 == len(pattern):
            parts.append("/.*")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile(r"\A" + "".join(parts) + r"\Z", re.S)


def matches(path: str, patterns: Iterable[str]) -> bool:
    """Whether ``path`` matches any of ``patterns``."""
    return any(glob_to_regex(pattern).match(path) for pattern in patterns)


def is_test_path(path: str) -> bool:
    """Whether ``path`` is a test by :data:`TEST_PATH_GLOBS`."""
    return matches(path, TEST_PATH_GLOBS)


def _paths_of(changed: ChangedFile) -> tuple[str, ...]:
    if changed.old_path is None:
        return (changed.path,)
    return (changed.path, changed.old_path)


def _first_match(changed: ChangedFile, patterns: Iterable[str]) -> str | None:
    globs = tuple(patterns)
    return next((p for p in _paths_of(changed) if matches(p, globs)), None)


def classify(changed: Sequence[ChangedFile]) -> UiChange:
    """Sort ``changed`` into UI, user-visible, test-only, contract and backend.

    One entry per changed file, in input order. A file is represented by its new
    path when that is under :data:`UI_PREFIX`, else by its old one.
    """
    ui_files: list[str] = []
    user_visible: list[str] = []
    test_only: list[str] = []
    api_contract: list[str] = []
    backend: list[str] = []
    for entry in changed:
        ui_path = next((p for p in _paths_of(entry) if p.startswith(UI_PREFIX)), None)
        if ui_path is not None and ui_path not in ui_files:
            ui_files.append(ui_path)
            if all(is_test_path(p) for p in _paths_of(entry)):
                test_only.append(ui_path)
            else:
                user_visible.append(ui_path)
        contract_path = _first_match(entry, API_CONTRACT_GLOBS)
        if contract_path is not None and contract_path not in api_contract:
            api_contract.append(contract_path)
        backend_path = _first_match(entry, BACKEND_GLOBS)
        if backend_path is not None and backend_path not in backend:
            backend.append(backend_path)
    return UiChange(
        ui_files=tuple(ui_files),
        user_visible=tuple(user_visible),
        test_only=tuple(test_only),
        api_contract=tuple(api_contract),
        backend=tuple(backend),
    )


def parse_name_status(raw: str) -> list[ChangedFile]:
    """Parse ``git diff --name-status -z -M`` output.

    NUL-separated: a status, then a path, then a second path for R and C entries,
    which git writes old path first. ``-z`` means paths arrive unquoted, so a
    space or a non-ASCII name needs no unescaping.
    """
    tokens = raw.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()
    changed: list[ChangedFile] = []
    index = 0
    while index < len(tokens):
        status = tokens[index]
        width = 3 if status[:1] in ("R", "C") else 2
        if not status or index + width > len(tokens):
            raise UiChangeError("git diff --name-status output is truncated")
        if width == 3:
            changed.append(
                ChangedFile(
                    status=status, path=tokens[index + 2], old_path=tokens[index + 1]
                )
            )
        else:
            changed.append(ChangedFile(status=status, path=tokens[index + 1]))
        index += width
    return changed


@dataclass
class _Head:
    """The first ``keep`` bytes of one of git's output streams, and its size."""

    keep: int
    data: bytearray = field(default_factory=bytearray)
    size: int = 0

    def drain(self, stream: IO[bytes]) -> None:
        """Read ``stream`` to its end and close it, keeping the first :attr:`keep`
        bytes and counting the rest."""
        with stream:
            while chunk := stream.read(_READ_BYTES):
                self.size += len(chunk)
                self.data += chunk[: max(self.keep - len(self.data), 0)]

    def text(self) -> str:
        """The kept bytes as ``subprocess``'s text mode reads them: UTF-8 with
        errors replaced, and universal newlines."""
        decoded = self.data.decode("utf-8", errors="replace")
        return decoded.replace("\r\n", "\n").replace("\r", "\n")


def _exited_by(popen: subprocess.Popen[bytes], deadline: float) -> bool:
    try:
        popen.wait(max(deadline - time.monotonic(), 0))
    except subprocess.TimeoutExpired:
        return False
    return True


def _kill(popen: subprocess.Popen[bytes], readers: Sequence[threading.Thread]) -> None:
    """Kill git and give each pipe :data:`_KILLED_PIPE_SECONDS` to close.

    A pipe closes at once unless a process git started still holds it, and then
    its reader is left to end with that process.
    """
    popen.kill()
    popen.wait()
    for reader in readers:
        reader.join(_KILLED_PIPE_SECONDS)


def _finished(
    popen: subprocess.Popen[bytes], readers: Sequence[threading.Thread], deadline: float
) -> bool:
    """Whether git exited and its pipes reached their end by ``deadline``; when
    they did not, git is killed (:func:`_kill`). So is git when the wait is
    interrupted, a job cancel included, before the interruption goes on."""
    try:
        for reader in readers:
            reader.join(max(deadline - time.monotonic(), 0))
        drained = not any(reader.is_alive() for reader in readers)
        if drained and _exited_by(popen, deadline):
            return True
    except BaseException:
        _kill(popen, readers)
        raise
    _kill(popen, readers)
    return False


def git_prefix(
    repo_dir: Path, *args: str, keep: int, timeout: float = 120.0
) -> tuple[str, int]:
    """Run ``git -C <repo_dir> <args>``; the first ``keep`` bytes it printed, as
    text, and how many bytes it printed in all.

    Whatever git prints past ``keep`` is read and dropped as it arrives, so this
    process holds at most ``keep`` bytes of it however much git prints, and git is
    never left blocked on a full pipe. Two threads read stdout and stderr, as
    ``Popen.communicate`` does on Windows, where ``ai_ux_test.py --collect-only``
    runs this too and no selector can wait on a pipe. Of stderr only the start is
    kept, for the error message.

    ``GIT_TERMINAL_PROMPT=0`` and a closed stdin make a fetch that wants
    credentials fail at once instead of waiting out the timeout for an answer
    nobody will type. git gets this process's environment without the variables
    :data:`_CREDENTIAL_ENV_PREFIXES` names, and nothing else is changed, so a host
    git's own setup (``HOME``, an SSH agent, a credential helper) still works.
    """
    command = args[0] if args else ""
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(_CREDENTIAL_ENV_PREFIXES)
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        popen = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            ["git", "-C", str(repo_dir), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
    except OSError as error:
        raise UiChangeError(f"git {command} could not be started: {error}") from error
    out, err = _Head(keep), _Head(_STDERR_BYTES)
    readers = [
        threading.Thread(target=head.drain, args=(stream,), daemon=True)
        for head, stream in ((out, popen.stdout), (err, popen.stderr))
        if stream is not None
    ]
    for reader in readers:
        reader.start()
    if not _finished(popen, readers, time.monotonic() + timeout):
        raise UiChangeError(f"git {command} did not finish within {timeout:g}s")
    if popen.returncode != 0:
        raise UiChangeError(f"git {command} failed: " + err.text().strip()[:400])
    return out.text(), out.size


def git(repo_dir: Path, *args: str, timeout: float = 120.0) -> str:
    """Run ``git -C <repo_dir> <args>`` as :func:`git_prefix` does; its stdout.

    A command that prints more than :data:`MAX_OUTPUT_BYTES` is refused with a
    :class:`UiChangeError` rather than cut, because a cut SHA or a cut list of
    changed files would be a wrong answer, not a short one.
    """
    limit = MAX_OUTPUT_BYTES
    text, size = git_prefix(repo_dir, *args, keep=limit, timeout=timeout)
    if size > limit:
        command = args[0] if args else ""
        raise UiChangeError(
            f"git {command} printed {size} bytes, more than the {limit} it is read to"
        )
    return text


def merge_base(
    repo_dir: Path, a: str, b: str, *, run: Callable[..., str] | None = None
) -> str:
    """The merge base of ``a`` and ``b``, as a full SHA.

    Against the merge base rather than the target tip, so commits that landed on
    the target after the branch started are not counted as the MR's.
    """
    try:
        sha = (run or git)(repo_dir, "merge-base", a, b).strip()
    except UiChangeError as error:
        raise UiChangeError(f"no merge base between {a} and {b}: {error}") from error
    if not _SHA_RE.fullmatch(sha):
        raise UiChangeError(f"git merge-base {a} {b} did not print a SHA")
    return sha


def changed_files(
    repo_dir: Path, base: str, head: str, *, run: Callable[..., str] | None = None
) -> list[ChangedFile]:
    """The files changed between ``base`` and ``head``, renames detected."""
    raw = (run or git)(repo_dir, "diff", "--name-status", "-z", "-M", f"{base}..{head}")
    return parse_name_status(raw)


def check_branch(name: str) -> str:
    """Return ``name`` if it is safe to place in a refspec, else raise.

    The target branch lands in ``+refs/heads/<t>:refs/ux-test/target-<t>``, so a
    colon, a space or ``..`` would change what the fetch writes.
    """
    if (
        not _BRANCH_RE.fullmatch(name)
        or ".." in name
        or "//" in name
        or name.endswith((".", "/", ".lock"))
    ):
        raise UiChangeError(f"not a branch name this job accepts: {name[:80]!r}")
    return name


def target_ref(branch: str) -> str:
    """The local ref a fetched target branch is written to."""
    return f"{REF_NAMESPACE}/target-{branch}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", required=True, help="where to write ui-change.json")
    parser.add_argument(
        "--repo-dir", default=".", help="the checkout to read (default: .)"
    )
    parser.add_argument(
        "--target-branch",
        default="develop",
        help="the MR's target branch (default: develop)",
    )
    parser.add_argument(
        "--remote", default="origin", help="where to fetch it from (default: origin)"
    )
    parser.add_argument(
        "--head", default="HEAD", help="the MR's head revision (default: HEAD)"
    )
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="diff against the local refs/heads/<target-branch> instead of fetching",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Classify the checkout's change against its target and write the JSON."""
    args = _parser().parse_args(argv)
    repo = Path(args.repo_dir)
    try:
        target = check_branch(args.target_branch)
        if args.no_fetch:
            base_ref = f"refs/heads/{target}"
        else:
            base_ref = target_ref(target)
            git(
                repo,
                "fetch",
                "--quiet",
                args.remote,
                f"+refs/heads/{target}:{base_ref}",
            )
        head_sha = git(repo, "rev-parse", "--verify", f"{args.head}^{{commit}}").strip()
        base_sha = merge_base(repo, base_ref, head_sha)
        change = classify(changed_files(repo, base_sha, head_sha))
        record = change.to_json(
            head_sha=head_sha, base_sha=base_sha, target_branch=target
        )
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    except (UiChangeError, OSError) as error:
        print(f"FAILED: {error}")
        return 1
    print(
        f"ui-change: {len(change.user_visible)} user-visible file(s), "
        f"skip={str(change.skip).lower()}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
