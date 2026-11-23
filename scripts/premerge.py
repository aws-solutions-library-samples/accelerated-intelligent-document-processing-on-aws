#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Run, locally, exactly the gate set both CIs run: ``make premerge``.

The gate set is not listed here. It is read from ``SHARED_GATES`` in
``scripts/tests/test_ci_gate_parity.py``, the authored list that test asserts both
the GitHub and the GitLab configuration invoke, so a gate added to CI and to that
list is run here with no edit to this file. The list is read with :mod:`ast` rather
than imported, because the module is a pytest file and importing it would pull in
pytest and its fixtures for a command that runs no test of its own.

Each entry is a *string CI is searched for*, not always a command that runs as it
stands. Two shapes are turned into a command mechanically -- ``make <target>``
runs that target from the repository root, and a ``scripts/...py`` path runs under
this interpreter -- and :data:`LOCAL_INVOCATIONS` holds the entries that need more
than that, each with its reason. Its keys are asserted to be members of
``SHARED_GATES`` by ``scripts/tests/test_premerge.py``, so an entry cannot outlive
the gate it describes, and an entry of neither shape with no invocation fails
before anything runs rather than being skipped.

Why it exists (#2115): a pre-merge run assembled by hand missed three gates CI
then failed on -- the SRT scan, a benchmark suite inside ``test-packages-cicd`` and
the coverage ratchet -- and the merged change turned ``develop`` red for every open
pull request. This runs every gate, in list order (the coverage ratchet reads the
reports the earlier steps write), carries on past a failure so one run reports all
of them, and exits non-zero if any failed.

What it does **not** reproduce: it runs on this machine, with this machine's
toolchain, against this working tree. CI runs the *merge result* of a pull request
with its base, on pinned images; a branch that is behind its base is the case where
the two answers differ, so merge the base in before running this. It also needs
what the gates need -- network access for ``terraform-check``, ``dep_audit.py`` and
the SRT setup, and Node for the UI suite and the type gate.

Usage::

    make premerge                      # run every gate
    python3 scripts/premerge.py --list # print the plan, run nothing
"""

from __future__ import annotations

import argparse
import ast
import os
import subprocess  # noqa: S404 - fixed argv lists, no shell
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_LIST_SOURCE = REPO_ROOT / "scripts" / "tests" / "test_ci_gate_parity.py"
GATE_LIST_NAME = "SHARED_GATES"


@dataclass(frozen=True)
class Step:
    """One command, run from ``cwd`` (relative to the repository root)."""

    argv: tuple[str, ...]
    cwd: str = "."
    env: dict[str, str] = field(default_factory=dict)


#: Gates whose CI search string is not a command that runs as written here. One
#: entry per gate, with the reason beside it.
LOCAL_INVOCATIONS: dict[str, tuple[Step, ...]] = {
    # The root Makefile has no `test-cicd`; CI runs the library's. SKIP_INSTALL=1
    # because without it the target reinstalls idp_common into whatever interpreter
    # `python3` is, which on a shared host repoints every other checkout (#1094).
    "make test-cicd": (
        Step(("make", "-C", "lib/idp_common_pkg", "test-cicd", "SKIP_INSTALL=1")),
    ),
    # Both CIs run `srt-setup` first; the scan has nothing to run without it. CI=1
    # is what makes the scan fail on a finding instead of reporting it, and keeps
    # the setup from waiting on a prompt.
    "make srt-scan": (
        Step(("make", "srt-setup"), env={"CI": "1"}),
        Step(("make", "srt-scan"), env={"CI": "1"}),
    ),
    # The UI suite runs from src/ui, against the locked dependencies.
    "npx vitest run": (
        Step(("npm", "ci", "--prefer-offline", "--no-audit"), cwd="src/ui"),
        Step(("npx", "vitest", "run", "--silent"), cwd="src/ui"),
    ),
}


def shared_gates(source: Path = GATE_LIST_SOURCE) -> list[str]:
    """``SHARED_GATES`` as that module assigns it, read without importing it."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if any(isinstance(t, ast.Name) and t.id == GATE_LIST_NAME for t in targets):
            value = ast.literal_eval(node.value)  # pyright: ignore[reportAttributeAccessIssue]
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise ValueError(f"{GATE_LIST_NAME} is not a list of strings")
            return value
    raise ValueError(f"{GATE_LIST_NAME} is not assigned in {source}")


def plan_for(gate: str) -> tuple[Step, ...]:
    """The local command(s) for one ``SHARED_GATES`` entry.

    Raises ``ValueError`` for an entry that is neither shape and has no entry in
    :data:`LOCAL_INVOCATIONS`, so a new kind of gate cannot be silently left out.
    """
    if gate in LOCAL_INVOCATIONS:
        return LOCAL_INVOCATIONS[gate]
    words = gate.split()
    if len(words) == 2 and words[0] == "make":
        return (Step(("make", words[1])),)
    if len(words) == 1 and gate.startswith("scripts/") and gate.endswith(".py"):
        return (Step((sys.executable, gate)),)
    raise ValueError(
        f"no local form for the CI gate {gate!r}: add one to LOCAL_INVOCATIONS "
        "in scripts/premerge.py"
    )


def full_plan(gates: list[str]) -> list[tuple[str, tuple[Step, ...]]]:
    return [(gate, plan_for(gate)) for gate in gates]


def _describe(step: Step) -> str:
    env = "".join(f"{k}={v} " for k, v in step.env.items())
    where = "" if step.cwd == "." else f"(cd {step.cwd} && "
    close = ")" if where else ""
    return f"{where}{env}{' '.join(step.argv)}{close}"


def run_gate(steps: tuple[Step, ...], root: Path = REPO_ROOT) -> int:
    """Run ``steps`` in order, stopping at the first that fails; its exit code."""
    for step in steps:
        print(f"\n$ {_describe(step)}", flush=True)
        try:
            result = subprocess.run(  # noqa: S603 - argv list, no shell
                list(step.argv),
                cwd=str(root / step.cwd),
                env={**os.environ, **step.env},
                check=False,
            )
        except OSError as error:
            print(f"cannot run {step.argv[0]}: {error}", file=sys.stderr)
            return 127
        if result.returncode != 0:
            return result.returncode
    return 0


def run(plan: list[tuple[str, tuple[Step, ...]]], root: Path = REPO_ROOT) -> int:
    """Run every gate, carrying on past failures; 1 if any failed, else 0."""
    results: list[tuple[str, int, float]] = []
    for gate, steps in plan:
        print(f"\n===== {gate} =====", flush=True)
        started = time.monotonic()
        code = run_gate(steps, root)
        results.append((gate, code, time.monotonic() - started))

    print("\n===== premerge summary =====")
    for gate, code, seconds in results:
        verdict = "PASS" if code == 0 else f"FAIL (exit {code})"
        print(f"  {verdict:<16} {seconds:7.0f}s  {gate}")
    failed = [gate for gate, code, _ in results if code != 0]
    if failed:
        print(f"\n{len(failed)} of {len(results)} CI gates failed.")
        return 1
    print(f"\nAll {len(results)} CI gates passed on this working tree.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--list", action="store_true", help="print the plan and run nothing"
    )
    args = parser.parse_args(argv)
    try:
        plan = full_plan(shared_gates())
    except (OSError, SyntaxError, ValueError) as error:
        print(f"premerge: {error}", file=sys.stderr)
        return 2
    if args.list:
        for gate, steps in plan:
            print(gate)
            for step in steps:
                print(f"    {_describe(step)}")
        return 0
    return run(plan)


if __name__ == "__main__":
    sys.exit(main())
