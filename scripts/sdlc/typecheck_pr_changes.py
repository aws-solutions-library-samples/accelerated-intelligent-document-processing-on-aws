#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Type check only the Python files changed on this branch — a DEVELOPER command.

**This is not a CI gate.** The gate is ``make typecheck``, which runs
``basedpyright`` over the whole tree (~47s) in both CI systems. This script exists
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
    # Try different git reference formats for CI compatibility
    ref_formats = [
        f"origin/{target_branch}...HEAD",  # Standard format
        f"origin/{target_branch}",  # Simple diff against target
        target_branch,  # Local branch if origin not available
    ]

    for ref in ref_formats:
        lines = _git_lines(["diff", "--name-only", ref])
        if lines is None:
            continue
        committed = _python_files(lines)
        return sorted(set(committed) | set(get_uncommitted_files()))

    # If all methods fail, print error
    print(
        f"❌ Error: Could not compare against target branch '{target_branch}'",
        file=sys.stderr,
    )
    print(f"Tried: {', '.join(ref_formats)}", file=sys.stderr)
    print("\nAvailable branches:", file=sys.stderr)
    try:
        subprocess.run(["git", "branch", "-a"], check=False)
    except Exception:
        pass
    sys.exit(1)


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
