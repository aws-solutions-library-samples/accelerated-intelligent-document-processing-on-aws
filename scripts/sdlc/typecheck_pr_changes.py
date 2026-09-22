#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Type check only the Python files changed on this branch — a DEVELOPER command.

**This is not a CI gate.** The gate is ``make typecheck``, which runs
``basedpyright`` over every tracked ``.py`` file in both CI systems.
This script exists
for local latency: it narrows the check to the files you are editing so the answer
comes back in a second or two.

Why it must not be the gate: a file-scoped check cannot see the break your change
caused somewhere it did not select. Change a function's signature and the error
appears at its *callers*, which are usually in files the diff does not touch. It
also never ran the check that ``pyrightconfig.json``'s ``include`` array is a
closure of — ``scripts/tests/test_pyright_config.py`` proves that array reaches
every tracked ``.py`` file, and for a long time nothing ran a check over it.

**How the file set is narrowed, and why not with a temporary config.** The files are
passed to ``basedpyright`` as command-line arguments, which replaces the ``include``
array while leaving every other setting in ``pyrightconfig.json`` in force. Writing a
temporary config with ``include`` overridden — the earlier approach — put a
``pyrightconfig.temp.json`` in the repo root and made the run's settings a copy that
could drift from the real one.

**The base ref is resolved and reported, not assumed.** The comparison asked for
``origin/<branch>`` first — and in this repository ``origin`` is the **GitLab
mirror**, while pull requests are opened against the ``github`` remote. So the first
candidate succeeded and the answer was wrong: the stale ref was a *remote-tracking*
one, not the local-branch fallback, which was never reached. Measured in one clone:
``origin/develop`` 79 commits behind ``github/develop`` (local ``develop`` 85 behind),
and **151** Python files selected where 10 had changed. Every commit merged in between
is attributed to the current branch, so the script reports type errors in other
people's merged work, which reads as a regression the developer just introduced. Two
sessions lost time to exactly that.

``resolve_base_ref`` now orders candidates by which remote a pull request is actually
diffed against, **warns and continues with the fresher ref** when the preferred one is
provably an ancestor of another candidate — it does not raise, because a correct base
is available in that situation — and prints the ref, SHA and date it settled on in
every case, including the one it cannot prove: a remote-tracking ref that has not
itself been fetched. A fork remote is never allowed to override a preferred one, since
a fork's ``develop`` can be ahead purely by carrying unmerged work.

**It cannot report success without having checked something.** Three routes to a
false pass are closed, and ``tests/test_typecheck_pr_changes.py`` drives each one:

1. ``basedpyright`` prints ``0 errors, 0 warnings, 0 notes`` and a
   ``filesAnalyzed`` of 0 when its file set resolves to nothing — a run that
   analysed nothing is textually identical to a clean run. So the analysed count
   is reconciled against the number of files selected, and a mismatch fails.
2. An unrecognised exit code is a failure, not a pass. A nonexistent path exits 4;
   a malformed config exits 3. Both used to be mapped to success.
3. Output that cannot be parsed is a failure. The summary used to be read out of
   prose with a regex, and an absent summary line reached a ``return 0``.

Usage:
    python scripts/sdlc/typecheck_pr_changes.py [target_branch]

    target_branch: Branch to compare against (default: develop)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

#: ``basedpyright`` exit codes this script knows how to interpret: 0 = it ran and
#: found no errors, 1 = it ran and reported errors. Everything else means it did
#: not do the job asked of it — 2 is a fatal internal error, 3 a config it could
#: not parse, 4 a path that does not exist — and must fail rather than be guessed
#: at. The previous ``return result.returncode if result.returncode in [0, 1]
#: else 0`` turned every one of those into a pass.
INTERPRETABLE_EXIT_CODES = (0, 1)


def _git_lines(args: list[str]) -> list[str] | None:
    """Run a git command and return its stdout lines, or None if it failed."""
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError:
        return None
    return result.stdout.splitlines()


def _python_files(lines: list[str]) -> list[str]:
    return [f for f in lines if f.endswith(".py") and Path(f).exists()]


def get_uncommitted_files() -> list[str]:
    """Changed Python files in the working tree — staged, unstaged, or untracked.

    Committed-only diffing makes this report success having checked nothing
    whenever it runs BEFORE a commit, which is exactly when a developer wants it.
    That is not hypothetical: it is how two `scope_allows is not defined` errors
    reached CI while the local run printed "No Python files changed".
    """
    collected: list[str] = []
    for args in (
        ["diff", "--name-only", "HEAD"],  # unstaged
        ["diff", "--name-only", "--cached"],  # staged
        ["ls-files", "--others", "--exclude-standard"],  # untracked
    ):
        lines = _git_lines(args)
        if lines:
            collected.extend(_python_files(lines))
    return sorted(set(collected))


#: Remotes to prefer when more than one carries the target branch, most
#: authoritative first. `github` is where pull requests are opened and merged, so
#: its remote-tracking ref is the base a PR will actually be diffed against;
#: `origin` is the GitLab mirror and can lag it by weeks.
#:
#: Hardcoding a single remote name is the defect this replaces: the previous
#: implementation asked for `origin/<branch>` first, which in this repository is the
#: mirror rather than the remote a PR is diffed against.
#:
#: Any other remote — a contributor's fork, a colleague's — sorts after both, and is
#: additionally never allowed to *override* a preferred remote below, because a
#: fork's `develop` can carry work that was never merged here.
#:
#: A remote NAME is used only to order candidates, never to decide whether one
#: exists: `_base_ref_candidates` derives that from the refs git actually has, so a
#: clone with neither of these remotes still works.
REMOTE_PREFERENCE = ("github", "origin")


def _ref_exists(ref: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        == 0
    )


def _is_strictly_behind(ref: str, other: str) -> bool:
    """True when `ref` is an ancestor of `other` and not the same commit.

    "Strictly" matters: two refs at the same commit are ancestors of each other,
    and reporting that as staleness would fire on every up-to-date clone.
    """
    same = subprocess.run(
        ["git", "rev-parse", f"{ref}^{{commit}}", f"{other}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = same.stdout.split()
    if same.returncode == 0 and len(lines) == 2 and lines[0] == lines[1]:
        return False
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", ref, other],
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        == 0
    )


def _base_ref_candidates(target_branch: str) -> list[str]:
    """Every ref that could serve as the comparison base, best first.

    Ordering is the whole fix. ANY of these refs can be arbitrarily stale, because a
    remote-tracking ref only moves on `git fetch` and a local branch only on a
    checkout and pull, so the question is which one is most likely to be the base a
    pull request is actually diffed against. Measured in this clone: `origin/develop`
    (the GitLab mirror) 79 commits behind `github/develop`, and local `develop` 85
    behind. Diffing against a stale base attributes every commit merged in between to
    the current branch, so the script reports type errors in other people's merged
    work and reads as a regression the developer just caused — 151 Python files
    selected against `origin/develop` where 10 had changed.
    """
    remotes = [line.strip() for line in (_git_lines(["remote"]) or []) if line.strip()]
    ordered = [r for r in REMOTE_PREFERENCE if r in remotes] + sorted(
        r for r in remotes if r not in REMOTE_PREFERENCE
    )
    candidates = [f"{remote}/{target_branch}" for remote in ordered]
    candidates.append(target_branch)
    return [ref for ref in candidates if _ref_exists(ref)]


def resolve_base_ref(target_branch: str = "develop") -> tuple[str | None, list[str]]:
    """Pick the base ref to diff against, and report it rather than assuming it.

    Returns `(ref, messages)`. `ref` is None when no candidate exists at all.
    `messages` always names the ref chosen, and additionally **warns** when the
    preferred ref is demonstrably stale — an ancestor of another candidate for the
    same branch.

    It **warns and continues with a better base**; it does not raise or exit. That is
    deliberate: there is a correct answer available in that situation (the fresher
    ref), so refusing to do the work would be worse than doing it and saying what was
    used. Nothing here is a gate — `make typecheck` is.

    Staleness is *demonstrable* only against a ref this clone already holds. A
    remote-tracking ref that has itself not been fetched recently cannot be
    detected without the network, which is why the chosen ref's SHA and date are
    printed unconditionally: the answer is then visible even in the case this
    cannot prove.
    """
    candidates = _base_ref_candidates(target_branch)
    if not candidates:
        return None, []

    chosen = candidates[0]
    messages: list[str] = []

    # A fresher candidate may override the preferred one only if it is itself from a
    # preferred remote or is the local branch. A fork's `develop` can be "ahead"
    # purely by carrying work that was never merged here, and taking it as the base
    # would silently narrow the diff — the opposite failure to the one being fixed.
    overridable = [
        ref
        for ref in candidates[1:]
        if ref == target_branch or ref.split("/", 1)[0] in REMOTE_PREFERENCE
    ]
    fresher = [ref for ref in overridable if _is_strictly_behind(chosen, ref)]
    if fresher:
        behind = _git_lines(["rev-list", "--count", f"{chosen}..{fresher[0]}"]) or ["?"]
        messages.append(
            f"⚠️  Preferred base ref '{chosen}' is {behind[0]} commit(s) behind "
            f"'{fresher[0]}'. Diffing against it would select every file changed by "
            f"the commits in between and report their type errors as yours, so "
            f"'{fresher[0]}' is being used instead. Either '{chosen}' has not been "
            f"fetched recently, or the two remotes genuinely differ — this repository "
            f"mirrors between two, and the mirror can lag. "
            f"'git fetch --all' settles which."
        )
        chosen = fresher[0]

    # The condition that actually caused the misreadings: the LOCAL branch is far
    # behind, and a developer reading "compared against develop" assumes it is not.
    # Not an error — the remote-tracking ref was used, so the answer is right — but
    # said out loud, because the local ref being stale is invisible otherwise.
    if chosen != target_branch and _ref_exists(target_branch):
        if _is_strictly_behind(target_branch, chosen):
            behind = _git_lines(
                ["rev-list", "--count", f"{target_branch}..{chosen}"]
            ) or ["?"]
            messages.append(
                f"ℹ️  Your local '{target_branch}' is {behind[0]} commit(s) behind "
                f"'{chosen}'. The remote-tracking ref was used, so the file list "
                f"below is your changes only; diffing against the local branch "
                f"would have added every file those commits touched."
            )

    described = _git_lines(["log", "-1", "--format=%h (%cs)", chosen]) or []
    messages.append(
        f"📍 Comparing against {chosen}" + (f" at {described[0]}" if described else "")
    )
    return chosen, messages


def get_changed_files(target_branch: str = "develop") -> list[str]:
    """Get list of changed Python files compared to target branch.

    The result is the union of what is COMMITTED on this branch and what is still
    in the working tree, so the answer does not depend on whether the developer
    has committed yet.

    Args:
        target_branch: Git branch to compare against

    Returns:
        List of Python file paths that have been modified
    """
    base, messages = resolve_base_ref(target_branch)
    for message in messages:
        print(message)

    if base is None:
        print(
            f"❌ Error: Could not compare against target branch '{target_branch}'",
            file=sys.stderr,
        )
        print(
            f"No ref matched '<remote>/{target_branch}' or '{target_branch}'.",
            file=sys.stderr,
        )
        print("\nAvailable branches:", file=sys.stderr)
        try:
            subprocess.run(["git", "branch", "-a"], check=False)
        except Exception:
            pass
        sys.exit(1)

    # `A...HEAD` (symmetric difference) rather than `A`: it diffs from the merge
    # base, so commits landing on the base after this branch started are not
    # attributed to it. With a stale base that distinction is what keeps other
    # people's work out of the file list even when the ref above could not be
    # proved stale.
    lines = _git_lines(["diff", "--name-only", f"{base}...HEAD"])
    if lines is None:
        lines = _git_lines(["diff", "--name-only", base]) or []
    return sorted(set(_python_files(lines)) | set(get_uncommitted_files()))


def _format_diagnostics(diagnostics: list[dict]) -> list[str]:
    """Render basedpyright's JSON diagnostics as the lines it would have printed."""
    lines: list[str] = []
    for item in diagnostics:
        start = (item.get("range") or {}).get("start") or {}
        where = item.get("file", "<unknown>")
        if "line" in start:
            where = f"{where}:{start['line'] + 1}:{start.get('character', 0) + 1}"
        rule = item.get("rule")
        suffix = f" ({rule})" if rule else ""
        severity = item.get("severity", "error")
        message = (item.get("message") or "").replace("\n", "\n    ")
        lines.append(f"  {where} - {severity}: {message}{suffix}")
    return lines


def interpret_result(
    files: list[str], returncode: int, stdout: str, stderr: str
) -> tuple[int, list[str]]:
    """Decide pass/fail from one ``basedpyright --outputjson`` invocation.

    Separated from the subprocess call so that every failure mode — including the
    ones that used to be silently mapped to success — is reachable from a test
    without a real type checker.

    Args:
        files: the files the caller asked to be checked. Its LENGTH is the
            contract: ``basedpyright`` must report having analysed exactly this
            many, or it did not check what was selected.
        returncode: the process exit status.
        stdout: the process's standard output, expected to be one JSON document.
        stderr: the process's standard error, used only for the failure message.

    Returns:
        ``(exit_code, lines_to_print)``. ``exit_code`` is 0 only when
        ``basedpyright`` ran, analysed exactly the requested files, and reported
        no errors.
    """
    output: list[str] = []

    if returncode not in INTERPRETABLE_EXIT_CODES:
        output.append(
            f"❌ basedpyright exited {returncode}, which does not mean "
            f"'checked, no errors' (0) or 'checked, errors found' (1). It did not "
            f"complete the check, so this is a failure rather than a pass."
        )
        if stderr.strip():
            output.append(f"   stderr: {stderr.strip()}")
        if stdout.strip():
            output.append(f"   stdout: {stdout.strip()[:2000]}")
        return 1, output

    try:
        report = json.loads(stdout)
        summary = report["summary"]
        analysed = int(summary["filesAnalyzed"])
        errors = int(summary["errorCount"])
        warnings = int(summary["warningCount"])
    except (ValueError, KeyError, TypeError) as exc:
        output.append(
            f"❌ Could not read basedpyright's JSON report ({exc}). Treating an "
            f"unreadable result as a failure: a summary this script cannot parse "
            f"is indistinguishable from a summary that says nothing was checked."
        )
        if stdout.strip():
            output.append(f"   stdout: {stdout.strip()[:2000]}")
        if stderr.strip():
            output.append(f"   stderr: {stderr.strip()}")
        return 1, output

    output.extend(_format_diagnostics(report.get("generalDiagnostics") or []))
    output.append(f"{errors} errors, {warnings} warnings ({analysed} files analysed)")

    if analysed != len(files):
        output.append(
            f"❌ basedpyright analysed {analysed} file(s) but {len(files)} were "
            f"selected, so its verdict does not cover them. A run that analysed "
            f"nothing reports '0 errors' exactly like a clean run, which is why "
            f"this is checked rather than trusted."
        )
        if stderr.strip():
            output.append(f"   stderr: {stderr.strip()}")
        output.append(
            "   Usual causes: a selected path basedpyright could not resolve, or "
            "one matched by pyrightconfig.json's `exclude`."
        )
        return 1, output

    return (1 if errors else 0), output


def run_type_check(files: list[str]) -> int:
    """Run basedpyright over exactly ``files`` and report whether they are clean.

    The files are passed as command-line arguments, which narrows the file set
    while leaving the rest of ``pyrightconfig.json`` in force.
    """
    # basedpyright is an npm devDependency of the root package.json, NOT installed
    # by `make setup` or `make setup-venv`. Without this guard the subprocess call
    # below raises FileNotFoundError and the gate reports a traceback instead of
    # the one-line remedy, which reads like a bug in this script.
    if shutil.which("basedpyright") is None:
        print(
            "❌ basedpyright is not on PATH, so no type checking was performed.\n"
            "   It is an npm devDependency of the root package.json and is not\n"
            "   installed by 'make setup' or 'make setup-venv'. Install it with:\n"
            "       npm install -g basedpyright\n"
            "   (this is what both CI systems do). If you used 'make setup-venv',\n"
            "   also run 'source .venv/bin/activate' so the other gate tools\n"
            "   resolve.",
            file=sys.stderr,
        )
        return 1

    result = subprocess.run(
        ["basedpyright", "--outputjson", *files],
        capture_output=True,
        text=True,
        check=False,
    )

    exit_code, lines = interpret_result(
        files, result.returncode, result.stdout, result.stderr
    )
    for line in lines:
        print(line)
    return exit_code


def main() -> int:
    """Main entry point for incremental type checking."""
    # `develop` is this repo's default branch, and get_changed_files() already
    # defaults to it — the two disagreeing meant a bare local run compared against
    # a branch that may not exist here.
    target_branch = sys.argv[1] if len(sys.argv) > 1 else "develop"

    print(f"🔍 Checking for Python files changed vs {target_branch}...")
    files = get_changed_files(target_branch)

    if not files:
        print(
            "✅ No Python files changed (committed or in the working tree) "
            "- skipping type check"
        )
        print(
            "   Note this is a narrow, local convenience — `make typecheck` is the "
            "gate, and it checks the whole tree."
        )
        return 0

    print(f"\n📝 Found {len(files)} changed Python file(s):")
    for f in files:
        print(f"  • {f}")

    print("\n🔬 Running type checks on changed files...\n")

    exit_code = run_type_check(files)

    if exit_code == 0:
        print("\n✅ Type checking passed for the changed files.")
        print("   `make typecheck` is the gate: run it before you push.")
    else:
        print("\n❌ Type checking failed - please fix the errors above")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
