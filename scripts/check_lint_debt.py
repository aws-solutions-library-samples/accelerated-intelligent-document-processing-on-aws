#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Keep ``ruff``'s exclusions finite, named, and honest.

WHAT THIS GATE IS FOR
---------------------
``ruff.toml`` used to exclude five **bare directory names** — ``notebooks``,
``options``, ``patterns``, ``src`` and ``scripts``. A bare name in ruff's
exclusion patterns matches at *any* path depth, and ``make ruff-lint`` /
``make format`` / ``make lint-cicd`` all invoke ruff with no path argument, so
the exclusions applied: 442 of 1230 tracked ``.py`` files were read by neither
the lint gate nor the formatter, among them all 138 files under ``scripts/``
(the repository's own hand-written gate layer -- 137 at its root plus one under
``security/threat-modeling/scripts/``) and 76 files under ``nested/*/src/``: 67
API-resolver Lambdas and 9 Bedrock Knowledge Base custom-resource handlers, which
nobody had counted because ``src`` matched there too. A clean ``ruff check`` on any
of those files meant the file was never opened. See issue #975.

Two properties are wanted at once, and they pull against each other:

1. a **new** Python file anywhere in the repository must be linted and
   format-checked by default, and
2. the pre-existing findings in the previously-unlinted trees must not have to
   be fixed in one commit — 267 lint findings and 189 unformatted files, 42 of
   those files being edited by five of the seven branches then open.

So the exclusions are now **per file**, generated, and ratcheted. ``ruff.toml``
names individual files, never a directory, so property 1 holds. This gate
supplies what naming a file cannot: it re-measures every tracked ``.py`` file
with the exclusions bypassed and fails if a listed file has *gained* a finding,
if a listed file is now *clean* (delist it — the ratchet only turns one way), or
if a listed path has disappeared.

The shape is borrowed from ``METERING_KEY_EXEMPT`` in
``lib/idp_common_pkg/tests/unit/bedrock/test_long_context_metering_key.py``,
whose comment states the principle this file implements: an exemption covers the
sites audited when it was written, not the filename forever.

WHAT IS NOT DEBT
----------------
``scripts/lint_debt.json``'s ``scope`` section is different in kind. Those two
entries are not debt to be paid down; they are decisions that ruff's rules do
not describe those files at all (vendored third-party source, Jupyter
notebooks). Each carries a ``premise`` naming a predicate below that is
evaluated against this tree, because the recurring defect in this repository is
an exemption whose stated reason is false for some member of the list it
justifies. Where a premise cannot make an exclusion finite, the entry says so in
``ratchetGap`` rather than implying a protection it does not have.

DISCOVERY GOES THROUGH GIT
--------------------------
Every file this gate measures comes from ``git ls-files``. It never walks the
filesystem: two gates in the last release cycle reported findings against build
output and against another branch's worktree under ``.claude/``, results CI
cannot reproduce. ``ruff`` itself *does* walk, so the last check here asserts
that its walk reaches nothing git ignores — which is how build output or a
nested worktree would get in.

A NOTE ON THE LINTER VERSION
---------------------------
Every number recorded here is a property of a ``ruff`` version. Both CI systems
pin the same one (asserted by ``scripts/tests/test_ci_gate_parity.py``), and the
failure output names the pin when the locally installed ``ruff`` differs, because
version skew and a real regression look identical in a count comparison. The
baseline was verified to be identical under the pinned version and the newest
release in the ``lib/idp_common_pkg`` range at the time it was recorded.

USAGE
-----
    python3 scripts/check_lint_debt.py             # the gate (make check-lint-debt)
    python3 scripts/check_lint_debt.py --write      # re-record the baseline
    python3 scripts/check_lint_debt.py --summary    # counts per tree, no verdict
    python3 scripts/check_lint_debt.py --explain P  # does any gate read P, and why not

Use ``--explain`` rather than asking ruff. Every ruff-native probe is misleading
for at least one class of file here -- see :func:`explain` -- and a misleading
probe is the stated reason #975 survived inspection for as long as it did.

``--write`` will not GROW either list. It refuses, naming the paths, unless given
``--allow-new-debt "<reason>"``, which records the reason in the baseline. Without
that refusal ``--write`` is a laundering step: the gate reddens on a new file with
findings and says "do not add the file to lintDebt", and ``--write`` would then add
it and turn everything green with the file permanently unlinted.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
RUFF_TOML = REPO_ROOT / "ruff.toml"
BASELINE = REPO_ROOT / "scripts" / "lint_debt.json"

#: Generated regions of ruff.toml. ``--write`` rewrites what is between each
#: pair; everything else in the file is hand-maintained.
BLOCK_BEGIN = "# >>> GENERATED {name} — edit scripts/lint_debt.json, then --write"
BLOCK_END = "# <<< GENERATED {name}"


# --------------------------------------------------------------------------- #
# Premises: facts about this tree that a scope exclusion claims
# --------------------------------------------------------------------------- #
def _premise_vendored_with_provenance(pattern: str) -> tuple[bool, str]:
    """A vendored tree is only vendored while it records what it was copied from.

    ``.claude/skills/sync-pii-anonymizer.md`` re-syncs this tree against the
    upstream commit pinned in its ``PROVENANCE.md``. Without that file the tree
    is no longer a tracked copy of someone else's code, and "not linted against
    our style" stops being a reason.
    """
    provenance = REPO_ROOT / pattern / "PROVENANCE.md"
    if not provenance.is_file():
        return False, f"{pattern}/PROVENANCE.md does not exist"
    return True, f"{pattern}/PROVENANCE.md pins the upstream commit"


def _premise_only_notebooks_and_they_have_their_own_gate(
    pattern: str,
) -> tuple[bool, str]:
    """This pattern must match only notebooks, and notebooks must be checked elsewhere.

    ``E402`` (imports not at the top), ``F811`` (redefinition) and ``I001``
    (import order) describe a module. A notebook is a document executed cell by
    cell, so those rules are not a quality signal there, and ``ruff format``
    would rewrite every cell's source array. Notebooks are not unchecked as a
    result: ``notebooks/_validate_notebooks.py`` resolves their imports and
    symbol references against a real environment.
    """
    matched = [p for p in tracked_files() if _matches_scope(p, pattern)]
    non_notebook = sorted(p for p in matched if not p.endswith(".ipynb"))
    if non_notebook:
        return False, (
            f"{pattern} also matches non-notebook files, which this reason does "
            f"not cover: {non_notebook[:5]}"
        )
    harness = REPO_ROOT / "notebooks" / "_validate_notebooks.py"
    if not harness.is_file():
        return False, (
            "notebooks/_validate_notebooks.py is gone, so notebooks would be "
            "checked by nothing at all"
        )
    return True, (
        f"{len(matched)} matched paths are all .ipynb, and "
        "notebooks/_validate_notebooks.py still checks them"
    )


PREMISES: dict[str, Callable[[str], tuple[bool, str]]] = {
    "vendored_with_provenance": _premise_vendored_with_provenance,
    "only_notebooks_and_they_have_their_own_gate": (
        _premise_only_notebooks_and_they_have_their_own_gate
    ),
}


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def _git(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


_TRACKED: list[str] | None = None


def tracked_files() -> list[str]:
    """Every path git tracks in this checkout. Never a filesystem walk."""
    global _TRACKED
    if _TRACKED is None:
        _TRACKED = sorted(_git("ls-files"))
    return _TRACKED


def tracked_python() -> list[str]:
    """Everything ruff would lint if nothing were excluded.

    ``.ipynb`` is included deliberately: ruff reads notebooks too, so leaving
    them out would make the ``scope`` entry that excludes them report that it
    shields nothing — an exemption whose own measurement says it is vacuous.
    """
    return [p for p in tracked_files() if p.endswith((".py", ".ipynb"))]


def _ruff() -> str:
    ruff = shutil.which("ruff")
    if ruff is None:
        sys.exit(
            "❌ ruff is not on PATH, so nothing was measured.\n"
            "   It lives in .venv/bin — run 'make setup-venv' and\n"
            "   'source .venv/bin/activate', or 'make setup' for a system install."
        )
    return ruff


def _ci_ruff_pin() -> str | None:
    """The ruff version CI installs, read from the workflow that installs it.

    A finding count is a property of a linter version, so this gate's verdict is
    too. If the running ruff is not the one CI pins, a mismatch here is far more
    likely to be that than a real change, and the failure output says so rather
    than leaving it to be guessed.
    """
    workflow = REPO_ROOT / ".github" / "workflows" / "developer-tests.yml"
    if not workflow.is_file():
        return None
    match = re.search(r"ruff==([0-9][0-9A-Za-z.\-]*)", workflow.read_text())
    return match.group(1) if match else None


def _running_ruff_version() -> str | None:
    result = subprocess.run(
        [_ruff(), "--version"], capture_output=True, text=True, check=False
    )
    parts = result.stdout.split()
    return parts[1] if len(parts) > 1 else None


def _run_ruff(args: list[str], paths: list[str]) -> str:
    """Run ruff over explicit paths, in chunks, and return the combined stdout.

    Explicit paths bypass ``exclude``/``extend-exclude``, which is the whole
    point: this gate needs to see what the exclusions are hiding. ``ruff.toml``
    is asserted not to set ``force-exclude``, which would defeat that.
    """
    out: list[str] = []
    chunk = 400
    for start in range(0, len(paths), chunk):
        result = subprocess.run(
            [_ruff(), *args, "--", *paths[start : start + chunk]],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        out.append(result.stdout)
    return "".join(out)


# Concise output is `path:line:col: RULE message`, except for a notebook, where
# ruff inserts the cell: `path.ipynb:cell 3:12:1: I001 ...`. The path group is
# non-greedy rather than "anything but whitespace or a colon", because a path
# containing a space is legal: with the old pattern such a file's findings simply
# did not match, so they were invisible to the ratchet while still failing a bare
# `ruff check`. Every parsed path is checked against the set that was asked about,
# so a path this still cannot parse is a hard error rather than a silent drop.
_FINDING_RE = re.compile(r"^(?P<path>.+?):(?:cell \d+:)?\d+:\d+: (?P<rule>[A-Z]+\d+)\b")


def measure() -> tuple[dict[str, Counter[str]], set[str]]:
    """Per-file lint findings by rule, and the set of unformatted files."""
    paths = tracked_python()
    wanted = set(paths)
    findings: dict[str, Counter[str]] = defaultdict(Counter)
    unresolved: list[str] = []
    for line in _run_ruff(
        ["check", "--no-fix", "--quiet", "--output-format", "concise"], paths
    ).splitlines():
        match = _FINDING_RE.match(line)
        if not match:
            continue
        if match["path"] not in wanted:
            unresolved.append(line)
            continue
        findings[match["path"]][match["rule"]] += 1

    if unresolved:
        sys.exit(
            "❌ could not attribute "
            f"{len(unresolved)} ruff finding(s) to a tracked file, so the ratchet "
            "would silently under-count. Fix the parser in "
            f"{Path(__file__).name} rather than ignoring these:\n  "
            + "\n  ".join(unresolved[:5])
        )

    unformatted = {
        line.removeprefix("Would reformat: ").strip()
        for line in _run_ruff(["format", "--check", "--quiet"], paths).splitlines()
        if line.startswith("Would reformat: ")
    }
    unknown = sorted(unformatted - wanted)
    if unknown:
        sys.exit(
            f"❌ `ruff format --check` named {len(unknown)} path(s) that are not in "
            f"the set asked about: {unknown[:5]}"
        )
    return dict(findings), unformatted


def ruff_walk() -> list[str]:
    """The files ruff discovers on its own, i.e. what ``make lint`` reads."""
    result = subprocess.run(
        [_ruff(), "check", "--show-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    root = str(REPO_ROOT) + "/"
    return [line.replace(root, "") for line in result.stdout.splitlines() if line]


def git_ignored(paths: list[str]) -> set[str]:
    """Which of ``paths`` git considers ignored (build output, local work)."""
    if not paths:
        return set()
    result = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=REPO_ROOT,
        input="\n".join(paths),
        capture_output=True,
        text=True,
        check=False,
    )
    return {line for line in result.stdout.splitlines() if line}


# --------------------------------------------------------------------------- #
# Baseline and ruff.toml
# --------------------------------------------------------------------------- #
def load_baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


def _matches_scope(path: str, pattern: str) -> bool:
    """Mirror the subset of ruff's pattern semantics the ``scope`` list uses.

    Two forms are understood: a directory prefix, and a suffix glob of the form
    ``**/*.ext``. A third form is not rejected by name — it simply matches
    nothing here, which check 4's non-vacuity assertion then reports as a scope
    entry that shields nothing. That is fail-closed but it names a different
    cause, so if a third form is ever wanted, teach this function about it rather
    than reading past the non-vacuity failure.
    """
    if pattern.startswith("**/*."):
        return path.endswith(pattern[4:])
    return path == pattern or path.startswith(pattern.rstrip("/") + "/")


def in_scope_exclusion(path: str, scope: dict) -> str | None:
    for pattern in scope:
        if _matches_scope(path, pattern):
            return pattern
    return None


def _toml_list(entries: list[str], indent: str = "    ") -> str:
    return "".join(f'{indent}"{entry}",\n' for entry in entries)


def _replace_block(text: str, name: str, body: str) -> str:
    begin = BLOCK_BEGIN.format(name=name)
    end = BLOCK_END.format(name=name)
    pattern = re.compile(re.escape(begin) + r"\n.*?" + re.escape(end), re.DOTALL)
    if not pattern.search(text):
        sys.exit(
            f"❌ ruff.toml has no generated block named {name!r}.\n"
            f"   Expected a line {begin!r} and a line {end!r}."
        )
    return pattern.sub(lambda _: f"{begin}\n{body}{end}", text)


def parse_ruff_lists() -> dict[str, list[str]]:
    """The three exclusion arrays, read out of ruff.toml as written."""
    text = RUFF_TOML.read_text(encoding="utf-8")
    lists: dict[str, list[str]] = {}
    for name in ("scope", "lint-debt", "format-debt"):
        begin = BLOCK_BEGIN.format(name=name)
        end = BLOCK_END.format(name=name)
        match = re.search(
            re.escape(begin) + r"\n(.*?)" + re.escape(end), text, re.DOTALL
        )
        lists[name] = re.findall(r'"([^"]*)"', match.group(1)) if match else []
    return lists


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.notes: list[str] = []

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def note(self, message: str) -> None:
        self.notes.append(message)


def check(report: Report) -> None:
    baseline = load_baseline()
    scope: dict = baseline["scope"]
    lint_debt: dict[str, dict[str, int]] = baseline["lintDebt"]
    format_debt: list[str] = baseline["formatDebt"]
    lists = parse_ruff_lists()
    tracked = set(tracked_python())

    # 1. force-exclude would make the measurement below blind.
    if re.search(r"^\s*force-exclude\s*=\s*true", RUFF_TOML.read_text(), re.MULTILINE):
        report.fail(
            "ruff.toml sets force-exclude = true. This gate measures the excluded "
            "files by naming them explicitly, which force-exclude defeats, so the "
            "ratchet would silently stop ratcheting."
        )

    # 2. No exclusion may be a bare directory name. This is the defect of #975:
    #    a bare name matches at any depth, so "src" also meant nested/*/src.
    for name, entries in lists.items():
        for entry in entries:
            if "/" not in entry:
                report.fail(
                    f"ruff.toml {name} entry {entry!r} has no '/', so ruff matches "
                    f"it at ANY path depth, not just at the repository root — the "
                    f"defect in issue #975. Name the path from the root."
                )

    # 3. ruff.toml and the baseline must agree, in both directions.
    for name, expected in (
        ("scope", sorted(scope)),
        ("lint-debt", sorted(lint_debt)),
        ("format-debt", sorted(format_debt)),
    ):
        if sorted(lists[name]) != expected:
            only_toml = sorted(set(lists[name]) - set(expected))
            only_json = sorted(set(expected) - set(lists[name]))
            report.fail(
                f"ruff.toml's generated {name!r} block and scripts/lint_debt.json "
                f"disagree.\n    only in ruff.toml: {only_toml}\n"
                f"    only in lint_debt.json: {only_json}\n"
                f"    Run: python3 scripts/check_lint_debt.py --write"
            )

    # 4. Every scope exclusion's premise is evaluated against this tree.
    for pattern, entry in scope.items():
        premise = entry.get("premise")
        if premise not in PREMISES:
            report.fail(
                f"scope entry {pattern!r} names premise {premise!r}, which is not "
                f"implemented in {Path(__file__).name}. Known: {sorted(PREMISES)}"
            )
            continue
        holds, detail = PREMISES[premise](pattern)
        if not holds:
            report.fail(
                f"the premise of scope exclusion {pattern!r} does not hold: {detail}\n"
                f"    Its stated reason is: {entry.get('reason', '(none)')}"
            )
        else:
            report.note(f"premise ok — {pattern}: {detail}")
        shielded = [p for p in tracked_python() if _matches_scope(p, pattern)]
        if not shielded:
            report.fail(
                f"scope exclusion {pattern!r} shields no tracked file ruff would "
                f"otherwise read, so it is dead. Remove it."
            )

    # 5. Every listed debt path must still exist and be tracked.
    for name, entries in (("lintDebt", sorted(lint_debt)), ("formatDebt", format_debt)):
        for path in entries:
            if path not in tracked:
                report.fail(
                    f"{name} lists {path!r}, which git does not track as a source file. "
                    f"A dead exclusion hides the next real one — drop the entry "
                    f"(python3 scripts/check_lint_debt.py --write)."
                )

    findings, unformatted = measure()

    # 6. The lint ratchet, per file and per rule.
    for path, recorded in sorted(lint_debt.items()):
        actual = findings.get(path, Counter())
        grew = {
            rule: (recorded.get(rule, 0), count)
            for rule, count in actual.items()
            if count > recorded.get(rule, 0)
        }
        if grew:
            detail = ", ".join(
                f"{rule}: was {was}, now {now}"
                for rule, (was, now) in sorted(grew.items())
            )
            report.fail(
                f"{path} is excluded from `ruff check` because it already had "
                f"findings, and it has GAINED some: {detail}.\n"
                f"    The exclusion covers the findings audited when it was "
                f"written, not the filename forever. Fix the new ones."
            )
        if not actual:
            report.fail(
                f"{path} is listed in lintDebt but `ruff check` now reports nothing "
                f"for it. Delist it so the file is linted from here on "
                f"(python3 scripts/check_lint_debt.py --write)."
            )
        elif sum(actual.values()) < sum(recorded.values()):
            report.note(
                f"{path}: {sum(recorded.values())} → {sum(actual.values())} findings; "
                f"re-record with --write to tighten the ratchet"
            )

    # 7. The format ratchet. Binary per file, so there is nothing to count.
    for path in sorted(format_debt):
        if path not in unformatted:
            report.fail(
                f"{path} is listed in formatDebt but `ruff format --check` is happy "
                f"with it now. Delist it "
                f"(python3 scripts/check_lint_debt.py --write)."
            )

    # 8. Nothing may carry findings without being listed — the direction that
    #    makes `make lint-cicd` green rather than merely quiet.
    for path in sorted(findings):
        if path in lint_debt or in_scope_exclusion(path, scope):
            continue
        report.fail(
            f"{path} has {sum(findings[path].values())} ruff finding(s) and is not "
            f"excluded, so `ruff check` fails on it. Fix them — do not add the file "
            f"to lintDebt, which is for the debt that predates issue #975."
        )
    for path in sorted(unformatted):
        if path in format_debt or in_scope_exclusion(path, scope):
            continue
        report.fail(
            f"{path} is not formatted, so `ruff format --check` fails. Run `make format`."
        )

    # 9. The recorded totals must match the lists, so growth of the LISTS is
    #    always a visible diff on three numbered lines rather than 200 quiet
    #    additions. `--write` refuses to grow them at all without
    #    --allow-new-debt "<reason>"; this is the half a static read can enforce.
    mark = baseline.get("highWaterMark")
    totals = _totals(lint_debt, format_debt)
    if mark is None:
        report.fail(
            "scripts/lint_debt.json has no highWaterMark. It records the size the "
            "exclusion lists are allowed to reach, and without it `--write` cannot "
            "tell growth from a re-record. Run: "
            "python3 scripts/check_lint_debt.py --write"
        )
    elif mark != totals:
        report.fail(
            f"scripts/lint_debt.json's highWaterMark {mark} does not match the "
            f"lists it describes {totals}. Either the lists were hand-edited (they "
            f"are generated — do not), or a re-record was left half-done. Run: "
            f"python3 scripts/check_lint_debt.py --write"
        )

    # 10. ruff's own walk must not reach anything git ignores.
    walked = ruff_walk()
    leaked = sorted(git_ignored(walked))
    if leaked:
        report.fail(
            f"`ruff check --show-files` reaches {len(leaked)} gitignored path(s), so "
            f"`make lint` reads build output or another checkout and CI cannot "
            f"reproduce the result: {leaked[:5]}\n"
            f"    Add the directory to ruff.toml's top-level `exclude`."
        )

    # 11. ...and it must reach everything git tracks that no scope entry covers.
    #     Check 2 reads only the three GENERATED blocks, so it cannot see the
    #     top-level `exclude` array -- which is bare directory names by design, for
    #     build output. Issue #975 is therefore re-openable through that array with
    #     every other check here green: adding "scripts" back to it drops the walk
    #     by 135 files while `ruff check` still prints "All checks passed!". This is
    #     the assertion that closes it, and it is a property of the walk rather than
    #     a list of names, so it covers a form nobody has thought of yet.
    walked_set = set(walked)
    unreached = sorted(
        path
        for path in tracked_python()
        if path not in walked_set and not in_scope_exclusion(path, scope)
    )
    if unreached:
        report.fail(
            f"`ruff` does not look at {len(unreached)} tracked file(s), and no "
            f"scripts/lint_debt.json `scope` entry accounts for them, so a NEW file "
            f"added beside any of them would be unlinted too: {unreached[:5]}\n"
            f"    This is the shape of issue #975. The cause is usually an entry in "
            f"ruff.toml's TOP-LEVEL `exclude` array — which is bare directory names "
            f"on purpose, for build output, so it must never name a source tree."
        )


def summarise() -> None:
    findings, unformatted = measure()
    baseline = load_baseline()
    scope = baseline["scope"]
    tracked = tracked_python()
    walked = {p for p in ruff_walk() if p.endswith((".py", ".ipynb"))}
    print(f"tracked .py/.ipynb files     : {len(tracked)}")
    print(f"examined by `ruff check`     : {len(walked)}")
    print(f"skipped by `ruff check`      : {len(tracked) - len(walked)}")
    print(f"files with findings (all)    : {len(findings)}")
    print(
        f"findings total (all)         : {sum(sum(c.values()) for c in findings.values())}"
    )
    print(f"unformatted (all)            : {len(unformatted)}")
    by_tree: Counter[str] = Counter()
    for path, counter in findings.items():
        by_tree[in_scope_exclusion(path, scope) or path.split("/")[0]] += sum(
            counter.values()
        )
    print("\nfindings by tree:")
    for tree, count in by_tree.most_common():
        print(f"  {count:5d}  {tree}")


def explain(path: str) -> int:
    """Say which gates read one file, and why not — the probe that cannot lie.

    Every ruff-native way of asking is misleading for at least one class of file,
    which is the same reason #975 survived inspection in the first place:

    * ``ruff check <path>`` and ``ruff format --check <path>`` bypass the
      exclusions entirely (an explicitly named path is not force-excluded by
      default), so they report on a file the gate never reads.
    * ``--force-exclude`` restores only ``exclude``/``extend-exclude``, which are
      *discovery* settings. ``lint.exclude`` and ``format.exclude`` filter after
      discovery, so ``ruff check --force-exclude <path>`` prints
      ``All checks passed!`` and exits 0 for all 85 lint-excluded files — the
      reassuring-and-false answer. For a format-excluded file
      ``ruff format --check --force-exclude`` prints *nothing at all*, not the
      ``No Python files found`` warning that the discovery-level exclusions give.
    * ``ruff check --show-files`` does not honour ``lint.exclude`` either, so a
      file appearing there is not evidence that it is linted.

    This reads the baseline, which is the source of truth for all three arrays.
    """
    baseline = load_baseline()
    scope: dict = baseline["scope"]
    rel = path
    if Path(path).is_absolute():
        try:
            rel = str(Path(path).resolve().relative_to(REPO_ROOT))
        except ValueError:
            print(f"{path} is outside {REPO_ROOT}")
            return 2
    tracked = set(tracked_files())
    if rel not in tracked:
        print(
            f"❓ {rel} is not tracked by git, so no gate in this repository reads it."
        )
        return 2

    pattern = in_scope_exclusion(rel, scope)
    lint_debt: dict = baseline["lintDebt"]
    print(f"{rel}")
    if pattern:
        entry = scope[pattern]
        print(f"  ruff check        : NOT read — scope exclusion {pattern!r}")
        print(f"  ruff format       : NOT read — scope exclusion {pattern!r}")
        print(f"  reason            : {entry.get('reason', '(none recorded)')}")
        print(f"  ratchet           : {entry.get('ratchet', '(none recorded)')}")
        print(f"  not protected by  : {entry.get('ratchetGap', '(none recorded)')}")
    else:
        counts = lint_debt.get(rel)
        if counts:
            shown = ", ".join(f"{rule}x{n}" for rule, n in sorted(counts.items()))
            print(
                f"  ruff check        : NOT read — in [lint] exclude, shielding {shown}"
            )
            print(
                "  ratchet           : a further finding in this file fails "
                "`make check-lint-debt`"
            )
        else:
            print("  ruff check        : read")
        if rel in baseline["formatDebt"]:
            print(
                "  ruff format       : NOT read — in [format] exclude (never formatted)"
            )
        else:
            print("  ruff format       : read")
    print(
        "  basedpyright      : "
        + (
            "read"
            if rel.endswith(".py")
            else "NOT read — notebooks are excluded (pyrightconfig.json)"
        )
    )
    return 0


def _totals(lint_debt: dict, format_debt: list) -> dict[str, int]:
    return {
        "lintDebtFiles": len(lint_debt),
        "lintFindings": sum(sum(counts.values()) for counts in lint_debt.values()),
        "formatDebtFiles": len(format_debt),
    }


def write(allow_new_debt: str | None = None) -> None:
    baseline = load_baseline()
    scope: dict = baseline["scope"]
    findings, unformatted = measure()

    lint_debt = {
        path: dict(sorted(counter.items()))
        for path, counter in sorted(findings.items())
        if not in_scope_exclusion(path, scope)
    }
    format_debt = sorted(
        path for path in unformatted if not in_scope_exclusion(path, scope)
    )

    # The list may only shrink. Without this, `--write` is a laundering step: the
    # gate correctly reddens on an unlisted file with findings and says "do not add
    # the file to lintDebt", and then --write adds it anyway and everything goes
    # green with the file permanently unlinted. The per-file ratchet turns one way;
    # this is what stops the *list itself* from growing.
    mark: dict = baseline.setdefault("highWaterMark", {})
    totals = _totals(lint_debt, format_debt)
    grown = {
        key: (mark[key], value)
        for key, value in totals.items()
        if key in mark and value > mark[key]
    }
    if grown and not allow_new_debt:
        detail = ", ".join(
            f"{k}: {was} → {now}" for k, (was, now) in sorted(grown.items())
        )
        added_lint = sorted(set(lint_debt) - set(baseline.get("lintDebt", {})))
        added_format = sorted(set(format_debt) - set(baseline.get("formatDebt", [])))
        sys.exit(
            f"❌ refusing to write: this would GROW the exclusion list ({detail}).\n"
            f"   newly excluded from `ruff check`:  {added_lint[:10] or 'none'}\n"
            f"   newly excluded from `ruff format`: {added_format[:10] or 'none'}\n\n"
            "   The lists only shrink. Fix the findings instead of recording them --\n"
            "   or, for a formatting entry, run `ruff format <that path>` with the\n"
            "   path spelled out, because bare `ruff format` honours the exclusion\n"
            "   and will skip the very file you are trying to fix. If the growth is\n"
            "   genuinely\n"
            "   unavoidable — a merge bringing in files another branch never\n"
            "   formatted, say — re-run with:\n\n"
            '       --write --allow-new-debt "why this could not be fixed here"\n\n'
            "   which records the reason in scripts/lint_debt.json so it is\n"
            "   reviewable rather than invisible."
        )
    if allow_new_debt:
        baseline.setdefault("newDebtJustifications", []).append(allow_new_debt)
    baseline["highWaterMark"] = totals
    for pattern, entry in scope.items():
        matched = [p for p in tracked_python() if _matches_scope(p, pattern)]
        entry["shields"] = {
            "files": len(matched),
            "lintFindings": sum(
                sum(findings.get(p, Counter()).values()) for p in matched
            ),
            "unformatted": sum(1 for p in matched if p in unformatted),
        }

    baseline["lintDebt"] = lint_debt
    baseline["formatDebt"] = format_debt
    BASELINE.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")

    text = RUFF_TOML.read_text(encoding="utf-8")
    text = _replace_block(text, "scope", _toml_list(sorted(scope)))
    text = _replace_block(text, "lint-debt", _toml_list(sorted(lint_debt)))
    text = _replace_block(text, "format-debt", _toml_list(sorted(format_debt)))
    RUFF_TOML.write_text(text, encoding="utf-8")

    print(
        f"✅ recorded {len(lint_debt)} lint-debt file(s) "
        f"({sum(sum(c.values()) for c in lint_debt.values())} findings) and "
        f"{len(format_debt)} unformatted file(s) into "
        f"{BASELINE.relative_to(REPO_ROOT)} and ruff.toml"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="re-record the baseline from the current tree (tightens the ratchet)",
    )
    parser.add_argument(
        "--allow-new-debt",
        metavar="REASON",
        help=(
            "with --write: permit the exclusion lists to GROW, recording REASON in "
            "the baseline. Without it, --write refuses to add a path — otherwise it "
            "launders a brand-new finding into a permanent exclusion"
        ),
    )
    parser.add_argument(
        "--summary", action="store_true", help="print the measurement, no verdict"
    )
    parser.add_argument(
        "--explain",
        metavar="PATH",
        help=(
            "say which gates read PATH and why not. Use this rather than "
            "`ruff check <path>` or --force-exclude, both of which answer "
            "misleadingly for a per-file exclusion"
        ),
    )
    parser.add_argument(
        "--verbose", action="store_true", help="also print the checks that passed"
    )
    args = parser.parse_args()

    if args.write:
        write(args.allow_new_debt)
        return 0
    if args.explain:
        return explain(args.explain)
    if args.summary:
        summarise()
        return 0

    report = Report()
    check(report)
    if args.verbose:
        for note in report.notes:
            print(f"  · {note}")
    if report.failures:
        print(f"❌ lint-debt gate: {len(report.failures)} problem(s)\n")
        for failure in report.failures:
            print(f"  - {failure}\n")
        pinned, running = _ci_ruff_pin(), _running_ruff_version()
        if pinned and running and pinned != running:
            print(
                f"NOTE: this ran ruff {running}, but CI pins ruff {pinned}. A "
                f"finding count is a property of the linter version, so a "
                f"mismatch above may be version skew rather than a code change. "
                f"Re-check with `pip install ruff=={pinned}` before re-recording."
            )
        print(
            "See scripts/check_lint_debt.py's module docstring and issue #975 for "
            "what this gate is protecting."
        )
        return 1
    baseline = load_baseline()
    print(
        f"✅ lint-debt gate: {len(baseline['lintDebt'])} file(s) excluded from "
        f"`ruff check` and {len(baseline['formatDebt'])} from `ruff format`, all "
        f"still carrying exactly the findings recorded for them; "
        f"{len(baseline['scope'])} scope exclusion(s) with a premise that holds."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
