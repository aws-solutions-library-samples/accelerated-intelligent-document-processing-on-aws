#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Fail when the committed English UI message catalog is stale, or a translation would fail the build.

The web UI declares every user-facing message next to the code that renders it
(react-intl ``defineMessages`` / ``<FormattedMessage>``: an explicit stable id, the
English inline as ``defaultMessage``, a ``description`` for the translator), and
``npm run i18n:extract`` (``formatjs extract``) collects them into the committed
``src/ui/src/i18n/en.json``. That file is the source every translation is made
from, and the coverage report and the catalog compiler both read it, so a stale
copy misreports coverage and lets a translation drift from the English it claims
to translate. Issue #1770.

Two checks, both check-only:

1. **English catalog currency.** Re-run the extraction into a temporary file with
   exactly the arguments of the ``i18n:extract`` script (read from
   ``src/ui/package.json``, so this gate cannot drift from the writer) and diff it
   against the committed ``en.json``. Any difference fails, printing the diff.
   The remedy is ``make ui-i18n-extract`` and committing the result; unlike
   ``codegen-check``, this gate never updates the file itself, so a local run and a
   CI run report the same thing about the same tree.

2. **Translation catalogs compile.** Every other ``src/ui/src/i18n/<locale>.json``
   is compiled against ``en.json`` by ``src/ui/scripts/i18n/check-catalog.mjs``,
   the same per-message check the Vite plugin applies at build time (ICU syntax,
   argument and plural/select parity, entry shape, no dead ids). A translation that
   would fail ``vite build`` therefore fails ``make lint`` first, with the same
   wording.

The extraction needs the UI's npm dependencies. Off CI, a checkout without
``src/ui/node_modules`` is an ordinary state (a Python-only change) and the gate
prints ``⏭️  check-ui-i18n-catalog SKIPPED`` and exits 0; that is a skip, never a
pass, and the message says what to run. In CI (``CI``, ``GITHUB_ACTIONS`` or
``GITLAB_CI`` set) the same condition is a broken job, because every CI job that
runs this gate installs the dependencies first (``make lint-cicd`` runs it right
after ``ui-lint``), and it fails. Same rule, and the same reason, as
``_require_node`` in ``scripts/tests/test_ui_test_discovery.py``.

Exit codes:
    0 — en.json is current and every translation catalog compiles (or skipped off CI)
    1 — en.json is stale, a translation catalog has a problem, the extraction failed,
        or the toolchain is missing in CI
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The npm script this gate replays. Its arguments are the single source of truth
#: for what is extracted; the gate only redirects ``--out-file``.
EXTRACT_SCRIPT = "i18n:extract"

#: The locale whose catalog is extracted, not translated.
SOURCE_LOCALE = "en"

#: A catalog file is ``<language>[-<Script>][-<REGION>].json``, as the Vite plugin reads it.
CATALOG_FILE = re.compile(
    r"^([a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|\d{3}))?)\.json$"
)

GATE = "check-ui-i18n-catalog"

#: Indirection for the tests, which replace it rather than running node.
run = subprocess.run


def in_ci() -> bool:
    """Both CI platforms set ``CI``; the other two cover a runner that sets only its own."""
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
    def catalog_dir(self) -> Path:
        return self.ui_root / "src" / "i18n"

    @property
    def en_catalog(self) -> Path:
        return self.catalog_dir / f"{SOURCE_LOCALE}.json"

    @property
    def formatjs(self) -> Path:
        return self.ui_root / "node_modules" / ".bin" / "formatjs"

    @property
    def check_catalog(self) -> Path:
        return self.ui_root / "scripts" / "i18n" / "check-catalog.mjs"

    def relative(self, path: Path) -> str:
        """``path`` as the failure messages name it: relative to the repository root."""
        try:
            return str(path.relative_to(self.repo_root))
        except ValueError:
            return str(path)


@dataclass
class Report:
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def extract_command(layout: Layout, out_file: Path) -> list[str]:
    """The ``i18n:extract`` command line with ``--out-file`` pointed at ``out_file``.

    Read from ``package.json`` rather than restated here, so the gate measures what
    the writer writes. The ``formatjs`` binary is the installed one, by path, so no
    ``npx`` can reach for the network.
    """
    scripts = json.loads(layout.package_json.read_text(encoding="utf-8"))["scripts"]
    try:
        words = shlex.split(scripts[EXTRACT_SCRIPT])
    except KeyError as exc:
        raise ValueError(
            f"{layout.relative(layout.package_json)} has no {EXTRACT_SCRIPT!r} script; "
            f"this gate replays it and cannot run without it"
        ) from exc
    if words[:2] != ["formatjs", "extract"] or "--out-file" not in words:
        raise ValueError(
            f"the {EXTRACT_SCRIPT!r} script is {scripts[EXTRACT_SCRIPT]!r}; this gate "
            f"expects `formatjs extract ... --out-file <path>` so it can redirect the output"
        )
    words[0] = str(layout.formatjs)
    words[words.index("--out-file") + 1] = str(out_file)
    return words


def catalog_locales(layout: Layout) -> list[str]:
    """Every locale with a ``<locale>.json`` in the catalog directory, sorted."""
    if not layout.catalog_dir.is_dir():
        return []
    found = []
    for path in layout.catalog_dir.iterdir():
        match = CATALOG_FILE.match(path.name)
        if match and path.is_file():
            found.append(match.group(1))
    return sorted(found)


def check_english_catalog(layout: Layout, report: Report) -> None:
    """Re-extract into a temporary file and diff it against the committed ``en.json``."""
    committed_path = layout.relative(layout.en_catalog)
    if not layout.en_catalog.is_file():
        report.failures.append(
            f"{committed_path} does not exist. Run 'make ui-i18n-extract' and commit "
            f"{committed_path}."
        )
        return
    with tempfile.TemporaryDirectory(prefix="ui-i18n-") as scratch:
        out_file = Path(scratch) / "en.json"
        command = extract_command(layout, out_file)
        report.notes.append(f"extraction: {shlex.join(command)}")
        result = run(
            command, cwd=layout.ui_root, capture_output=True, text=True, check=False
        )
        if result.returncode != 0 or not out_file.is_file():
            report.failures.append(
                f"`formatjs extract` failed (exit {result.returncode}); the English "
                f"catalog could not be re-measured. Its output:\n"
                f"{(result.stdout + result.stderr).strip()}"
            )
            return
        fresh = out_file.read_text(encoding="utf-8")
    committed = layout.en_catalog.read_text(encoding="utf-8")
    if fresh == committed:
        report.notes.append(
            f"{committed_path} matches a fresh extraction "
            f"({len(json.loads(committed))} message(s))"
        )
        return
    diff = "".join(
        difflib.unified_diff(
            committed.splitlines(keepends=True),
            fresh.splitlines(keepends=True),
            fromfile=f"{committed_path} (committed)",
            tofile=f"{committed_path} (re-extracted from src/ui/src)",
        )
    )
    report.failures.append(
        f"{committed_path} is stale: the messages declared in src/ui/src differ from "
        f"the committed catalog.\n{diff.rstrip()}\n"
        f"Run 'make ui-i18n-extract' and commit {committed_path}"
    )


def check_translation_catalogs(layout: Layout, report: Report) -> None:
    """Compile every ``<locale>.json`` other than ``en.json`` as the build would."""
    locales = [loc for loc in catalog_locales(layout) if loc != SOURCE_LOCALE]
    if not locales:
        report.notes.append("no translation catalogs yet; only en.json exists")
        return
    for locale in locales:
        command = ["node", str(layout.check_catalog), locale]
        result = run(
            command, cwd=layout.ui_root, capture_output=True, text=True, check=False
        )
        output = (result.stdout + result.stderr).strip()
        if result.returncode != 0:
            report.failures.append(
                f"{layout.relative(layout.catalog_dir / f'{locale}.json')} would fail "
                f"the UI build (exit {result.returncode}):\n{output}"
            )
        else:
            report.notes.append(output or f"{locale}.json compiles")


def check(layout: Layout, report: Report) -> None:
    check_english_catalog(layout, report)
    check_translation_catalogs(layout, report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verbose", action="store_true", help="also print the checks that passed"
    )
    args = parser.parse_args(argv)
    layout = Layout(REPO_ROOT)

    if not layout.formatjs.is_file():
        where = layout.relative(layout.formatjs)
        if in_ci():
            print(
                f"❌ {GATE}: {where} is missing, so the English catalog cannot be "
                f"re-measured. In CI this is a job-configuration failure, not a reason "
                f"to skip: every job that runs this gate installs the UI's npm "
                f"dependencies first (`make lint-cicd` runs it after `ui-lint`). Give "
                f"the job the dependencies; do not relax this to a skip."
            )
            return 1
        print(f"⏭️  {GATE} SKIPPED (not a pass: run make ui-lint first)")
        print(
            f"   {where} is missing, so the English catalog was not re-measured and no "
            f"translation catalog was compiled."
        )
        return 0

    report = Report()
    try:
        check(layout, report)
    except (OSError, ValueError, KeyError) as exc:
        report.failures.append(str(exc))
    if args.verbose:
        for note in report.notes:
            print(f"  · {note}")
    if report.failures:
        print(f"❌ {GATE}: {len(report.failures)} problem(s)\n")
        for failure in report.failures:
            print(f"  - {failure}\n")
        print(
            "See scripts/check_ui_i18n_catalog.py's module docstring and issue #1770 "
            "for what this gate is protecting."
        )
        return 1
    translations = [loc for loc in catalog_locales(layout) if loc != SOURCE_LOCALE]
    print(
        f"✅ {GATE}: {layout.relative(layout.en_catalog)} matches a fresh extraction "
        f"and {len(translations)} translation catalog(s) compile against it"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
