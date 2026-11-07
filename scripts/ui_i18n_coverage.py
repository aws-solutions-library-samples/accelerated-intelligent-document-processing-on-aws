#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Report how much of the web UI each translation catalog covers. Advisory: it never fails.

``make ui-i18n-coverage`` runs ``src/ui/scripts/i18n/coverage.mjs`` over
``src/ui/src/i18n`` and prints its report. For every ``<locale>.json`` other than
``en.json`` that is: the English ids the catalog lacks (``missing``); the entries
whose English changed since they were translated (``stale``: the ``sourceHash``
each entry records no longer matches the current ``defaultMessage``, and since
ids are stable nothing else would show that an outdated translation is still
being served); the entries the build's parity check rejects (``invalid``); the
ids ``en.json`` no longer has (``dead``); and a coverage percentage. Issue #1770.

WHY THIS NEVER FAILS (decision D17 of #1770)
---------------------------------------------
A missing or stale translation is an English fallback on screen, not a broken
build, and the screens are converted one at a time. A gate over coverage would be
red for the whole series and read by nobody, or it would turn every English-only
string into a blocking check. So the report is advisory by design: this wrapper
exits 0 whatever the report says; the make target sits under "UI Development", is
not a prerequisite of ``lint`` or ``fastlint``, is not in the ``lint-cicd``
recipe, and its name matches none of the check shapes
``scripts/tests/test_ci_gate_parity.py`` classifies by. The one class that is
enforced, ``invalid``, is enforced elsewhere: ``vite build`` and
``make check-ui-i18n-catalog`` fail on it. Here it is listed so the figure is read
for what it is. The percentage is what each release's notes quote once ``fr-CA``
is enabled.

HOW IT RUNS
-----------
``node`` by name from ``PATH`` (the Node the developer or the CI job already has;
never ``npx``, never the network) over the worker by path, with the catalog
directory by path. ``--json`` and ``--locale <tag>`` are forwarded as they are,
and the worker's stdout and stderr are printed as they are, so
``make ui-i18n-coverage UI_I18N_COVERAGE_ARGS=--json`` prints JSON and nothing
else. The worker needs ``@formatjs/icu-messageformat-parser`` out of
``src/ui/node_modules`` for the parity check. Off CI, a checkout without it is an
ordinary state (a Python-only change) and this prints
``⏭️  ui-i18n-coverage SKIPPED`` and exits 0; that is a skip, not a report, and
the message says what to run. In CI (``CI``, ``GITHUB_ACTIONS`` or ``GITLAB_CI``
set) the same condition is a broken job and exits 2, the rule
``scripts/check_ui_i18n_catalog.py`` and ``_require_node`` in
``scripts/tests/test_ui_test_discovery.py`` apply.

Exit codes:
    0 — the report was printed, whatever it says (or skipped off CI)
    2 — no report could be produced: the worker failed (an unreadable catalog
        directory or file, malformed JSON, a ``--locale`` with no catalog file),
        ``node`` could not be started, or the toolchain is missing in CI

USAGE
-----
    python3 scripts/ui_i18n_coverage.py                  # make ui-i18n-coverage
    python3 scripts/ui_i18n_coverage.py --json           # machine-readable
    python3 scripts/ui_i18n_coverage.py --locale fr-CA   # one catalog only
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

TOOL = "ui-i18n-coverage"

#: The exit status for "no report could be produced", the worker's own code for it.
TOOL_ERROR = 2

#: Indirection for the tests, which replace it rather than running node.
run = subprocess.run


def in_ci() -> bool:
    """Both CI platforms set ``CI``; the other two cover a runner that sets only its own.

    Mirrors ``in_ci`` in ``scripts/check_ui_i18n_catalog.py`` (the precedent for a
    script that needs the UI's node toolchain) rather than importing it: ``scripts/``
    is not a package, and these scripts are run by path.
    """
    return bool(
        os.environ.get("CI")
        or os.environ.get("GITHUB_ACTIONS")
        or os.environ.get("GITLAB_CI")
    )


@dataclass(frozen=True)
class Layout:
    """Where the report reads, derived from one root so the tests can point it elsewhere."""

    repo_root: Path

    @property
    def ui_root(self) -> Path:
        return self.repo_root / "src" / "ui"

    @property
    def catalog_dir(self) -> Path:
        return self.ui_root / "src" / "i18n"

    @property
    def worker(self) -> Path:
        return self.ui_root / "scripts" / "i18n" / "coverage.mjs"

    @property
    def parser_package(self) -> Path:
        """The one installed package the worker imports (through ``validate-message.mjs``)."""
        return self.ui_root / "node_modules" / "@formatjs" / "icu-messageformat-parser"

    def relative(self, path: Path) -> str:
        """``path`` as the messages name it: relative to the repository root."""
        try:
            return str(path.relative_to(self.repo_root))
        except ValueError:
            return str(path)


def worker_command(
    layout: Layout, *, as_json: bool = False, locale: str | None = None
) -> list[str]:
    """The ``node`` command line for the worker, flags forwarded as given."""
    command = ["node", str(layout.worker), str(layout.catalog_dir)]
    if as_json:
        command.append("--json")
    if locale is not None:
        command += ["--locale", locale]
    return command


def report(layout: Layout, *, as_json: bool = False, locale: str | None = None) -> int:
    """Run the worker and print what it printed. 0 when it reported; 2 when it could not."""
    command = worker_command(layout, as_json=as_json, locale=locale)
    try:
        result = run(
            command, cwd=layout.ui_root, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        print(f"❌ {TOOL}: node could not be started: {exc}", file=sys.stderr)
        return TOOL_ERROR
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.returncode != 0:
        print(
            f"❌ {TOOL}: no report could be produced (node exited "
            f"{result.returncode}). That is a tool error, not a coverage finding: "
            f"the report itself never fails (decision D17 of #1770).",
            file=sys.stderr,
        )
        return TOOL_ERROR
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--json", action="store_true", help="print the report as JSON, for a machine"
    )
    parser.add_argument(
        "--locale", metavar="TAG", help="report on this locale's catalog only"
    )
    args = parser.parse_args(argv)
    layout = Layout(REPO_ROOT)

    if not layout.parser_package.is_dir():
        where = layout.relative(layout.parser_package)
        if in_ci():
            print(
                f"❌ {TOOL}: {where} is missing, so no translation can be measured "
                f"against the English. In CI this is a job-configuration failure, not "
                f"a reason to skip: every job that runs this installs the UI's npm "
                f"dependencies first. Give the job the dependencies; do not relax "
                f"this to a skip."
            )
            return TOOL_ERROR
        # On stderr like every other non-report path here: a caller piping stdout for
        # the report (or its --json form) must get nothing, not a human notice.
        print(
            f"⏭️  {TOOL} SKIPPED (not a pass: run make ui-lint first)", file=sys.stderr
        )
        print(
            f"   {where} is missing, so no translation catalog was measured.",
            file=sys.stderr,
        )
        return 0

    return report(layout, as_json=args.json, locale=args.locale)


if __name__ == "__main__":
    sys.exit(main())
