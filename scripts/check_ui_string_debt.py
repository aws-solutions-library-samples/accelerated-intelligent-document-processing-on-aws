#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Ratchet the web UI's hardcoded user-facing strings: per file, the count may only fall.

WHAT THIS GATE IS FOR
---------------------
The UI is being internationalised (issue #1770) one screen at a time, and the tree
carries thousands of English strings written straight into JSX text, props, object
literals and plain ``.ts`` modules. Converting them all in one change is not an
option, and a lint rule that fails on every one of them would be red forever and
read by nobody. So the rule is run as a *measurement* and the result is ratcheted:

* ``src/ui/eslint.i18n.config.js`` is THE COUNTING RULE — ``eslint-plugin-i18next``'s
  ``no-literal-string`` in mode ``all``, with exclude lists for leaf attributes,
  technical object keys, logger/constructor callees and technical word patterns. It
  is run through ``npm run i18n:lint`` and is deliberately not part of
  ``npm run lint`` (which runs with ``--max-warnings 0`` inside ``npm run build``).
* ``scripts/ui_string_debt.json`` is the baseline: for every file with at least one
  finding, the number of findings **pinned** for it when the baseline was written,
  plus the sha256 of the counting configuration those numbers were measured with.
  That configuration is the config file AND the pinned ``eslint-plugin-i18next``
  version from ``src/ui/package.json``: what the rule counts and skips (the
  ``const ALL_CAPS`` skip below is the plugin's, not the config's) changes with the
  plugin, so a bump re-records the baseline the way an edited exclude list does.

The gate re-measures the tree and fails when

1. a listed file carries MORE findings than its pinned count (it grew);
2. a file that is NOT listed carries any finding at all — a new file and a converted
   file both have an implicit baseline of zero, and that is what keeps a finished
   screen finished;
3. a listed file now carries ZERO findings — the entry is **vacuous** and must be
   deleted (run ``--write``), so the file is held at zero from here on rather than
   being allowed to drift back up to its old count;
4. a listed path no longer exists on disk — a stale entry hides the next real one;
5. the counting configuration's sha256 differs from the one recorded — a widened
   exclude list, or a bumped ``eslint-plugin-i18next``, silently changes every count,
   so it has to be a visible ``--write`` in the same change, reviewable beside the
   edit that caused it;
6. the recorded ``highWaterMark`` does not match the list it describes — the list is
   generated, and growth of the list must show up as a diff on two numbered lines.

The direction matters. A per-file count can go down freely (``--write`` re-records
it; the gate only notes the improvement until then), but it cannot go up, and the
list of files cannot gain a member: ``--write`` REFUSES to add a path or raise a
count unless given ``--allow-new-debt "<reason>"``, which it records in
``newDebtReasons`` so the exception is reviewable rather than invisible. Without
that refusal ``--write`` is a laundering step — the gate reddens on a new hardcoded
string, and one command turns it green with the string permanently uncounted.

WHAT IS NOT COUNTED, STATED PLAINLY
-----------------------------------
Two gaps are known and declared rather than implied away (decision D20 of #1770):

a. ``eslint-plugin-i18next`` skips every string inside a ``const ALL_CAPS = ...``
   declaration unconditionally (``isUpperCase`` in its ``VariableDeclarator``
   visitor; it is not an option). Measured on the tree this was written against by
   running the same rule and lists with that skip neutralised: **679 strings inside
   184 ALL-CAPS declarations in 78 files** are not counted, 552 of them inside
   object or array literals under user-facing keys (``COLUMN_DEFINITIONS_MAIN``,
   ``FORMAT_OPTIONS``, ``METRIC_CONFIGS``, ``LANGUAGE_CODES`` ...), 77 bare string
   constants (``const DISMISS_LABEL = 'Dismiss'``), the rest inside arrow functions
   and templates. They are paid down as the conversion PRs replace those constants
   with ``defineMessages`` descriptors and render-time formatting; the gap is
   revisited if it is still large after them.
b. ``new Error('...')`` texts are not counted, by design: ``Error`` and every
   ``*Error`` constructor are excluded callees, because those messages are logged
   and shown through ``.message`` paths that a stable-error-code change (decision
   D25) will address separately.

A single lowercase token (``'primary'``, ``'error'``, ``'documents'``) is also not
counted: the word list sets aside ``[a-z][a-zA-Z0-9]*`` because the overwhelming
majority of such strings are variants, statuses and keys. That is a property of the
counting configuration rather than of the plugin, and it is listed here so the
number is read for what it is.

HOW THE MEASUREMENT RUNS
------------------------
The ESLint binary is ``src/ui/node_modules/.bin/eslint``, by path — never ``npx``,
which can reach for the network — with exactly the arguments of the ``i18n:lint``
script in ``src/ui/package.json`` (read, not restated, so the gate cannot drift from
the script a developer runs by hand). ESLint exits 1 when it has findings, which is
the expected state; exit 2 is a tool error and fails the gate with ESLint's stderr.
A message with no rule id (a parse error, an unknown rule in a directive comment)
is a hard error too, because a file ESLint could not read would otherwise count as
clean.

Off CI, a checkout without ``src/ui/node_modules`` is an ordinary state (a
Python-only change), and the gate prints ``⏭️  check-ui-string-debt SKIPPED`` and
exits 0; that is a skip, never a pass, and the message says what to run. In CI
(``CI``, ``GITHUB_ACTIONS`` or ``GITLAB_CI`` set) the same condition is a broken
job and fails: every CI job that runs this gate installs the UI's dependencies
first (``make lint-cicd`` runs it right after ``ui-lint``). Same rule, same reason,
as ``scripts/check_ui_i18n_catalog.py`` and ``_require_node`` in
``scripts/tests/test_ui_test_discovery.py``.

USAGE
-----
    python3 scripts/check_ui_string_debt.py              # the gate (make check-ui-string-debt)
    python3 scripts/check_ui_string_debt.py --write       # re-record the baseline (shrinks only)
    python3 scripts/check_ui_string_debt.py --summary     # totals and the top offenders, no verdict
    python3 scripts/check_ui_string_debt.py --explain P   # P's pinned count, current count, every finding
    python3 scripts/check_ui_string_debt.py --verbose     # also print the checks that passed

Paying a file down: convert its strings (``defineMessages`` / ``<FormattedMessage>``,
see ``src/ui/src/i18n/README.md``), then ``--write``. The entry shrinks or, at zero,
disappears, and the file is held there.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

GATE = "check-ui-string-debt"

#: The npm script this gate replays. Its arguments are the single source of truth
#: for what is counted; the gate only swaps the binary for the installed one by path.
LINT_SCRIPT = "i18n:lint"

#: The one rule whose messages are counted. Anything else ESLint reports is a tool
#: problem, not a finding.
RULE_ID = "i18next/no-literal-string"

#: Where the fix is documented (written in the docs commit of the same series).
README = "src/ui/src/i18n/README.md"

#: Findings printed per file in a failure before pointing at ``--explain``.
SHOWN_PER_FILE = 25

#: Indirection for the tests, which replace it rather than running node.
run = subprocess.run


def in_ci() -> bool:
    """Both CI platforms set ``CI``; the other two cover a runner that sets only its own.

    Mirrors ``in_ci`` in ``scripts/check_ui_i18n_catalog.py`` (the precedent for a
    gate that needs the UI's node toolchain) rather than importing it: ``scripts/`` is
    not a package, and the gates are run by path.
    """
    return bool(
        os.environ.get("CI")
        or os.environ.get("GITHUB_ACTIONS")
        or os.environ.get("GITLAB_CI")
    )


@dataclass(frozen=True)
class Layout:
    """Where the gate reads, derived from one root so the tests can point it elsewhere."""

    repo_root: Path

    @property
    def ui_root(self) -> Path:
        return self.repo_root / "src" / "ui"

    @property
    def package_json(self) -> Path:
        return self.ui_root / "package.json"

    @property
    def eslint(self) -> Path:
        return self.ui_root / "node_modules" / ".bin" / "eslint"

    @property
    def baseline(self) -> Path:
        return self.repo_root / "scripts" / "ui_string_debt.json"

    def relative(self, path: Path) -> str:
        """``path`` as the messages name it: relative to the repository root."""
        try:
            return str(path.resolve().relative_to(self.repo_root.resolve()))
        except ValueError:
            return str(path)


@dataclass
class Report:
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Finding:
    """One counted string: where it is and what it says."""

    path: str
    line: int
    column: int
    text: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}:{self.column}  {self.text}"


# --------------------------------------------------------------------------- #
# The measurement
# --------------------------------------------------------------------------- #
def lint_command(layout: Layout) -> tuple[list[str], str]:
    """The ``i18n:lint`` command line with the installed ESLint by path, and its config.

    Read from ``package.json`` rather than restated here, so the gate measures what
    ``npm run i18n:lint`` measures. A script of another shape is refused rather than
    misread: the gate needs ``--no-config-lookup`` (so the main configuration cannot
    leak in), ``--config <file>`` (the counting rule, whose hash the baseline pins)
    and JSON output (what it parses). The configuration path is returned relative to
    the repository root, which is how the baseline records it and the messages name it.
    """
    scripts = json.loads(layout.package_json.read_text(encoding="utf-8"))["scripts"]
    try:
        words = shlex.split(scripts[LINT_SCRIPT])
    except KeyError as exc:
        raise ValueError(
            f"{layout.relative(layout.package_json)} has no {LINT_SCRIPT!r} script; "
            f"this gate replays it and cannot run without it"
        ) from exc
    well_formed = (
        words[:1] == ["eslint"]
        and "--no-config-lookup" in words
        and "--config" in words
        and words.index("--config") + 1 < len(words)
        and (
            (
                "-f" in words
                and words[words.index("-f") + 1 : words.index("-f") + 2] == ["json"]
            )
            or (
                "--format" in words
                and words[words.index("--format") + 1 : words.index("--format") + 2]
                == ["json"]
            )
        )
    )
    if not well_formed:
        raise ValueError(
            f"the {LINT_SCRIPT!r} script is {scripts[LINT_SCRIPT]!r}; this gate expects "
            f"`eslint --no-config-lookup --config <file> ... -f json <globs>` so it can "
            f"pin the configuration and parse the output"
        )
    config = layout.relative(layout.ui_root / words[words.index("--config") + 1])
    words[0] = str(layout.eslint)
    return words, config


#: The plugin whose rule does the counting. Its pinned version is part of the counting
#: configuration the baseline is measured with: the rule's own behaviour (what it
#: reaches, what it skips unconditionally) changes between versions, so a bump has to
#: re-record the baseline as visibly as an edited exclude list does.
COUNTING_PLUGIN = "eslint-plugin-i18next"


def counting_plugin_pin(layout: Layout) -> str:
    """The version ``src/ui/package.json`` pins for the counting plugin.

    Read from the committed manifest rather than from ``node_modules``, so the hash is a
    function of the tree and not of what happens to be installed; refused when absent,
    because a count whose rule is unpinned is a count nobody can reproduce.
    """
    package = json.loads(layout.package_json.read_text(encoding="utf-8"))
    for section in ("devDependencies", "dependencies"):
        pin = package.get(section, {}).get(COUNTING_PLUGIN)
        if pin:
            return str(pin)
    raise ValueError(
        f"{layout.relative(layout.package_json)} does not pin {COUNTING_PLUGIN}, whose "
        f"rule does the counting; the baseline cannot record a counting configuration "
        f"without its version"
    )


def config_sha256(layout: Layout, config: str) -> str:
    """The sha256 the baseline records: the counting configuration file (repo-relative)
    followed by the pinned counting plugin, ``<name>@<version>``, on a line of its own."""
    digest = hashlib.sha256((layout.repo_root / config).read_bytes())
    digest.update(f"\n{COUNTING_PLUGIN}@{counting_plugin_pin(layout)}".encode("utf-8"))
    return digest.hexdigest()


def _excerpt(source: str, message: dict) -> str:
    """The reported node's source text, on one line and bounded, for a human."""
    lines = source.split("\n")
    line, column = message["line"], message["column"]
    end_line = message.get("endLine", line)
    end_column = message.get("endColumn", column + 1)
    try:
        if end_line == line:
            text = lines[line - 1][column - 1 : end_column - 1]
        else:
            parts = [lines[line - 1][column - 1 :]]
            parts.extend(lines[line : end_line - 1])
            parts.append(lines[end_line - 1][: end_column - 1])
            text = "\n".join(parts)
    except IndexError:
        text = ""
    text = " ".join(text.split()) or message.get("message", "")
    if len(text) > 80:
        text = text[:77] + "..."
    if message.get("nodeType") == "JSXText":
        # A string literal's source text carries its own quotes; JSX text does not.
        text = f"'{text}'"
    return text


def measure(layout: Layout) -> dict[str, list[Finding]]:
    """Run the counting configuration; every finding, per repo-relative path.

    Only files with at least one finding appear. ESLint exit 1 means findings and is
    the expected state; exit 2 (or anything else) is a tool error.
    """
    argv, _ = lint_command(layout)
    result = run(argv, cwd=layout.ui_root, capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"`{shlex.join(argv)}` exited {result.returncode}, so nothing was "
            f"measured. ESLint said:\n{(result.stderr or result.stdout).strip()}"
        )
    try:
        results = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"ESLint's output was not the JSON the {LINT_SCRIPT!r} script asks for "
            f"({exc}). Its stderr:\n{result.stderr.strip()}"
        ) from exc

    findings: dict[str, list[Finding]] = {}
    unreadable: list[str] = []
    for entry in results:
        path = layout.relative(Path(entry["filePath"]))
        source = entry.get("source") or ""
        for message in entry.get("messages", []):
            if message.get("ruleId") != RULE_ID:
                unreadable.append(
                    f"{path}:{message.get('line', 0)}: {message.get('message')}"
                )
                continue
            findings.setdefault(path, []).append(
                Finding(
                    path, message["line"], message["column"], _excerpt(source, message)
                )
            )
    if unreadable:
        raise RuntimeError(
            f"ESLint reported {len(unreadable)} message(s) that are not {RULE_ID} "
            f"findings — a parse error or an unknown rule in a directive comment — so a "
            f"file it could not read would count as clean. Fix these rather than "
            f"ignoring them:\n  " + "\n  ".join(unreadable[:10])
        )
    return findings


def counts(findings: dict[str, list[Finding]]) -> dict[str, int]:
    return {path: len(items) for path, items in sorted(findings.items()) if items}


def high_water_mark(debt: dict[str, int]) -> dict[str, int]:
    return {"files": len(debt), "findings": sum(debt.values())}


# --------------------------------------------------------------------------- #
# The baseline
# --------------------------------------------------------------------------- #
BASELINE_COMMENT = [
    "The hardcoded user-facing strings the UI string-debt gate agrees not to fail on,",
    "per file. Consumed by scripts/check_ui_string_debt.py, which re-measures the",
    "tree with src/ui/eslint.i18n.config.js (eslint-plugin-i18next, no-literal-string,",
    "mode all) and fails when a listed file carries more findings than recorded here,",
    "when an unlisted file carries any, when a listed file is at zero (delete the",
    "entry), when a listed path is gone, or when the counting configuration's sha256",
    "(the config file plus the eslint-plugin-i18next version src/ui/package.json pins)",
    "no longer matches configSha256. Issue #1770.",
    "",
    "GENERATED. `stringDebt`, `highWaterMark` and `configSha256` are written by",
    "`python3 scripts/check_ui_string_debt.py --write`; do not hand-edit them. The",
    "list only shrinks: --write refuses to add a path or raise a count unless given",
    "--allow-new-debt '<reason>', which it appends to newDebtReasons.",
    "",
    "Paying a file down: declare its strings with defineMessages / <FormattedMessage>",
    "(src/ui/src/i18n/README.md), then run --write. At zero the entry disappears and",
    "the file is held there, like every file that is not listed.",
]


def load_baseline(layout: Layout) -> dict:
    return json.loads(layout.baseline.read_text(encoding="utf-8"))


def _render(
    baseline: dict,
    *,
    config: str,
    sha256: str,
    debt: dict[str, int],
    reasons: list[str],
) -> dict:
    """The baseline document in its fixed key order."""
    return {
        "$comment": baseline.get("$comment", BASELINE_COMMENT),
        "generator": "python3 scripts/check_ui_string_debt.py --write",
        "eslintConfig": config,
        "configSha256": sha256,
        "highWaterMark": high_water_mark(debt),
        "newDebtReasons": reasons,
        "stringDebt": dict(sorted(debt.items())),
    }


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #
def _show(items: list[Finding]) -> str:
    shown = "\n".join(f"      {item}" for item in items[:SHOWN_PER_FILE])
    if len(items) > SHOWN_PER_FILE:
        shown += (
            f"\n      ... and {len(items) - SHOWN_PER_FILE} more "
            f"(python3 scripts/check_ui_string_debt.py --explain {items[0].path})"
        )
    return shown


def check(layout: Layout, report: Report) -> None:
    baseline = load_baseline(layout)
    debt: dict[str, int] = baseline.get("stringDebt", {})
    _, config = lint_command(layout)

    # 5. The counting configuration must be the one the numbers were measured with.
    recorded_sha = baseline.get("configSha256")
    actual_sha = config_sha256(layout, config)
    if baseline.get("eslintConfig") != config or recorded_sha != actual_sha:
        report.failures.append(
            f"{config} with {COUNTING_PLUGIN}@{counting_plugin_pin(layout)} (sha256 "
            f"{actual_sha[:12]}...) is not the counting configuration "
            f"{layout.relative(layout.baseline)} was measured with "
            f"({str(recorded_sha)[:12]}...). A changed exclude list, or a bumped "
            f"{COUNTING_PLUGIN}, changes every count below it, so re-record in the same "
            f"change — python3 scripts/check_ui_string_debt.py --write — and review the "
            f"baseline diff beside the configuration or dependency diff."
        )
    else:
        report.notes.append(f"{config} matches configSha256")

    # 6. The recorded totals must match the list, so growth of the LIST is a diff on
    #    two numbered lines rather than quiet additions.
    mark = baseline.get("highWaterMark")
    if mark != high_water_mark(debt):
        report.failures.append(
            f"{layout.relative(layout.baseline)}'s highWaterMark {mark} does not match "
            f"the stringDebt list it describes {high_water_mark(debt)}. Either the list "
            f"was hand-edited (it is generated — do not) or a re-record was left "
            f"half-done. Run: python3 scripts/check_ui_string_debt.py --write"
        )

    findings = measure(layout)
    measured = counts(findings)

    # 1, 3, 4. Every listed file: not grown, not at zero, still there.
    for path, pinned in sorted(debt.items()):
        actual = measured.get(path, 0)
        if actual > pinned:
            report.failures.append(
                f"{path} carries {actual} hardcoded user-facing string(s); {pinned} were "
                f"recorded when its entry was written, so it has GAINED {actual - pinned}. "
                f"The entry covers the strings audited when it was written, not the file "
                f"forever: declare the new one(s) with defineMessages / <FormattedMessage> "
                f"instead. Its findings now:\n{_show(findings[path])}"
            )
        elif actual == 0:
            if (layout.repo_root / path).is_file():
                report.failures.append(
                    f"{path} is listed with {pinned} finding(s) but now has none, so its "
                    f"entry is vacuous and would let the file drift back up to {pinned}. "
                    f"Delete it so the file is held at zero "
                    f"(python3 scripts/check_ui_string_debt.py --write)."
                )
            else:
                report.failures.append(
                    f"{path} is listed but no longer exists, so its entry is stale. A dead "
                    f"entry hides the next real one — drop it "
                    f"(python3 scripts/check_ui_string_debt.py --write)."
                )
        elif actual < pinned:
            report.notes.append(
                f"{path}: {pinned} → {actual} finding(s); re-record with --write to "
                f"tighten the ratchet"
            )

    # 2. Nothing may carry findings without being listed: new files and converted
    #    files have an implicit baseline of zero.
    for path in sorted(measured):
        if path in debt:
            continue
        report.failures.append(
            f"{path} is not in the baseline (implicit count 0) and carries "
            f"{measured[path]} hardcoded user-facing string(s). Declare them with "
            f"defineMessages / <FormattedMessage>; do not add the file to the baseline, "
            f"which is for the debt that predates issue #1770:\n{_show(findings[path])}"
        )


def summarise(layout: Layout, top: int = 20) -> None:
    findings = measure(layout)
    measured = counts(findings)
    baseline = load_baseline(layout) if layout.baseline.is_file() else {}
    debt: dict[str, int] = baseline.get("stringDebt", {})
    print(f"files with findings    : {len(measured)}")
    print(f"findings total         : {sum(measured.values())}")
    print(f"baseline files         : {len(debt)}")
    print(f"baseline findings      : {sum(debt.values())}")
    improved = sum(1 for p, n in debt.items() if measured.get(p, 0) < n)
    print(f"files below their pin  : {improved}")
    print(f"\ntop {top} offenders (current / pinned):")
    for path, count in sorted(measured.items(), key=lambda item: (-item[1], item[0]))[
        :top
    ]:
        print(f"  {count:5d} / {debt.get(path, 0):5d}  {path}")


def explain(layout: Layout, path: str) -> int:
    """One file: its pinned count, its current count and every current finding."""
    candidate = Path(path)
    rel = layout.relative(candidate) if candidate.is_absolute() else path
    if not (layout.repo_root / rel).is_file() and (layout.ui_root / path).is_file():
        rel = layout.relative(layout.ui_root / path)
    baseline = load_baseline(layout) if layout.baseline.is_file() else {}
    pinned = baseline.get("stringDebt", {}).get(rel)
    findings = measure(layout).get(rel, [])
    print(rel)
    print(f"  baseline count : {pinned if pinned is not None else '0 (not listed)'}")
    print(f"  current count  : {len(findings)}")
    if not (layout.repo_root / rel).is_file():
        print("  note           : this path does not exist in the checkout")
        return 2
    if findings:
        print("  findings:")
        for item in findings:
            print(f"    {item}")
    return 0


def write(layout: Layout, allow_new_debt: str | None = None) -> None:
    """Re-record the baseline from the current tree. The list may only shrink."""
    _, config = lint_command(layout)
    bootstrap = not layout.baseline.is_file()
    baseline = {} if bootstrap else load_baseline(layout)
    old_debt: dict[str, int] = baseline.get("stringDebt", {})
    new_debt = counts(measure(layout))

    added = sorted(set(new_debt) - set(old_debt))
    raised = sorted(
        path
        for path, count in new_debt.items()
        if path in old_debt and count > old_debt[path]
    )
    if (added or raised) and not allow_new_debt and not bootstrap:
        old_mark, new_mark = high_water_mark(old_debt), high_water_mark(new_debt)
        sys.exit(
            f"❌ refusing to write: this would GROW the string-debt baseline "
            f"(files {old_mark['files']} → {new_mark['files']}, findings "
            f"{old_mark['findings']} → {new_mark['findings']}).\n"
            f"   newly listed files:   {added[:10] or 'none'}\n"
            f"   raised counts:        "
            + (
                ", ".join(f"{p}: {old_debt[p]} → {new_debt[p]}" for p in raised[:10])
                or "none"
            )
            + "\n\n"
            "   The baseline only shrinks. Declare the strings with defineMessages /\n"
            f"   <FormattedMessage> (see {README}) instead of recording them. If the\n"
            "   growth is genuinely unavoidable — a merge bringing in a screen another\n"
            "   branch wrote before the ratchet, say — re-run with:\n\n"
            '       --write --allow-new-debt "why this could not be converted here"\n\n'
            "   which records the reason in scripts/ui_string_debt.json so it is\n"
            "   reviewable rather than invisible."
        )
    reasons: list[str] = list(baseline.get("newDebtReasons", []))
    if allow_new_debt and (added or raised):
        reasons.append(allow_new_debt)

    document = _render(
        baseline,
        config=config,
        sha256=config_sha256(layout, config),
        debt=new_debt,
        reasons=reasons,
    )
    layout.baseline.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    mark = high_water_mark(new_debt)
    print(
        f"✅ recorded {mark['files']} file(s) carrying {mark['findings']} hardcoded "
        f"user-facing string(s) into {layout.relative(layout.baseline)}"
        + (" (first baseline)" if bootstrap else "")
    )


def main(argv: list[str] | None = None) -> int:
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
            "with --write: permit the baseline to GROW, recording REASON in it. Without "
            "it, --write refuses to add a path or raise a count — otherwise it launders "
            "a brand-new hardcoded string into the permanent baseline"
        ),
    )
    parser.add_argument(
        "--summary", action="store_true", help="print the measurement, no verdict"
    )
    parser.add_argument(
        "--explain",
        metavar="PATH",
        help="print PATH's pinned count, current count and every current finding",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="also print the checks that passed"
    )
    args = parser.parse_args(argv)
    layout = Layout(REPO_ROOT)

    if not layout.eslint.is_file():
        where = layout.relative(layout.eslint)
        if in_ci():
            print(
                f"❌ {GATE}: {where} is missing, so the UI's hardcoded strings cannot be "
                f"counted. In CI this is a job-configuration failure, not a reason to "
                f"skip: every job that runs this gate installs the UI's npm dependencies "
                f"first (`make lint-cicd` runs it after `ui-lint`). Give the job the "
                f"dependencies; do not relax this to a skip."
            )
            return 1
        print(f"⏭️  {GATE} SKIPPED (not a pass: run make ui-lint first)")
        print(f"   {where} is missing, so no string was counted.")
        return 0

    try:
        if args.write:
            write(layout, args.allow_new_debt)
            return 0
        if args.explain:
            return explain(layout, args.explain)
        if args.summary:
            summarise(layout)
            return 0

        report = Report()
        check(layout, report)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"❌ {GATE}: {exc}")
        return 1

    if args.verbose:
        for note in report.notes:
            print(f"  · {note}")
    if report.failures:
        print(f"❌ {GATE}: {len(report.failures)} problem(s)\n")
        for failure in report.failures:
            print(f"  - {failure}\n")
        print(
            f"How to declare a user-facing string, and how to pay a file down: {README}. "
            f"What this gate protects: scripts/check_ui_string_debt.py's module "
            f"docstring and issue #1770."
        )
        return 1
    baseline = load_baseline(layout)
    mark = baseline.get("highWaterMark", {})
    print(
        f"✅ {GATE}: {mark.get('files', 0)} file(s) still carry exactly the "
        f"{mark.get('findings', 0)} hardcoded user-facing string(s) recorded for them "
        f"(or fewer), no unlisted file carries any, and the counting configuration is "
        f"the one they were measured with."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
