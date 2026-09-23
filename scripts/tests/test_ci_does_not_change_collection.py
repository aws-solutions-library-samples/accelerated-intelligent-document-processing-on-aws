# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A test suite must not collect a different set of cases because it is in CI.

``lib/idp_common_pkg/tests/unit/extraction/agentic_idp/conftest.py`` began its skip
decision with ``if os.getenv("CI"): return True``. Both CIs set ``CI`` — GitHub
Actions and GitLab each do — so 64 cases covering the agentic extraction path were
dropped during collection on every pull request, while running normally on every
developer machine. The packages they need were installed in both jobs the whole time;
nothing was missing. A change could break those 64 and go green (#1175).

**Why nothing noticed for so long.** Cases dropped by ``pytest_ignore_collect`` are
not reported as skips, so both sides printed the same skip count and the pytest
summary line was identical in shape. What eventually surfaced it was indirect and
looked like something else entirely: the per-file coverage ratchet started failing on
``extraction/agentic_idp.py``, ``extraction/service.py`` and ``schema/__init__.py``
— the three modules those tests are what covers — simultaneously on every open branch,
with no change to any of them. Read as a coverage regression it is a puzzle; read as
a baseline recorded where the tests run and checked where they do not, it is obvious.

So this gate is deliberately about the **class** and not that one directory: any
conftest anywhere under the unit suite that makes collection conditional on the
environment fails here, whatever mechanism it uses. A static check for ``os.getenv``
would have been cheaper and would only have covered the one spelling.

There is no exemption list, and that is a measured claim rather than an omission: no
test or conftest in the tree reads ``CI`` at all today. The two places that do are
``scripts/srt/setup.py`` and ``scripts/srt/run.py``, which use it to choose
non-interactive behaviour and do not decide what is collected. If a genuinely
CI-conditional case ever arrives, it needs an entry in
``scripts/tests/gate_exemptions.json`` — a test that cannot run in CI is exactly the
kind of thing that should be written down rather than inferred from a conftest.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT_SUITE = REPO_ROOT / "lib/idp_common_pkg/tests/unit"
AGENTIC_DIR_FRAGMENT = "extraction/agentic_idp/"

#: Every first-party root, because `tests/unit`'s conftest imports across them.
_FIRST_PARTY_ROOTS = (
    "lib/idp_common_pkg",
    "lib/idp_sdk",
    "lib/idp_cli_pkg",
    "lib/idp_feature_sdk",
    "",
)


def _env(*, ci: bool) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        str(REPO_ROOT / root) if root else str(REPO_ROOT) for root in _FIRST_PARTY_ROOTS
    )
    if ci:
        env["CI"] = "true"
    else:
        # Popped rather than set empty: the old condition was truthiness of the
        # value, so `CI=""` would have read as "not CI" and measured nothing.
        env.pop("CI", None)
        env.pop("GITHUB_ACTIONS", None)
        env.pop("GITLAB_CI", None)
    return env


def _collect(*, ci: bool) -> set[str]:
    """Node ids ``pytest --collect-only`` reports for the unit suite."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(UNIT_SUITE),
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=REPO_ROOT,
        env=_env(ci=ci),
        capture_output=True,
        text=True,
        timeout=900,
    )
    return {line.strip() for line in result.stdout.splitlines() if "::" in line}


@pytest.fixture(scope="module")
def collected() -> tuple[set[str], set[str]]:
    without_ci = _collect(ci=False)
    with_ci = _collect(ci=True)
    if not without_ci and not with_ci:
        pytest.skip(
            "pytest collected nothing from lib/idp_common_pkg/tests/unit in either "
            "environment, so this gate has nothing to compare. Install the test "
            "extras (`cd lib/idp_common_pkg && pip install -e '.[test]'`) and re-run."
        )
    return without_ci, with_ci


@pytest.mark.unit
def test_setting_ci_does_not_change_what_the_unit_suite_collects(collected):
    """The class assertion: collection is a property of the tree, not of the runner."""
    without_ci, with_ci = collected

    dropped = without_ci - with_ci
    added = with_ci - without_ci

    def _per_module(node_ids: set[str]) -> str:
        counts = Counter(node_id.split("::")[0] for node_id in node_ids)
        return "\n".join(
            f"    {count:4}  {module}" for module, count in sorted(counts.items())
        )

    assert not dropped, (
        f"{len(dropped)} case(s) are collected without CI set and NOT collected with "
        f"it, so CI runs a smaller suite than a developer does and a failure in them "
        f"cannot reach a pull request:\n{_per_module(dropped)}\n"
        "A conftest is making collection conditional on the environment. Make it "
        "conditional on the dependency it actually needs, or register the exemption "
        "in scripts/tests/gate_exemptions.json."
    )
    assert not added, (
        f"{len(added)} case(s) are collected ONLY with CI set, so they never run "
        f"locally and a developer cannot reproduce a CI failure in "
        f"them:\n{_per_module(added)}"
    )


@pytest.mark.unit
def test_the_comparison_reaches_the_directory_that_was_skipped(collected):
    """Non-vacuity, bounded to what this environment can actually answer.

    The assertion above passes trivially in an environment where the agentic tests
    are correctly absent — no ``strands``, no ``pyarrow`` — so on a machine that has
    both, this pins that the comparison really does span the directory whose
    exclusion was the defect. Without it the gate could go green on a tree that had
    stopped collecting those cases in *both* environments, which is the same
    64-tests-never-run outcome by a different route.
    """
    for package in ("strands", "pyarrow"):
        pytest.importorskip(
            package,
            reason=(
                f"{package} is not installed, so the agentic_idp tests are correctly "
                "not collected in either environment and there is nothing to pin"
            ),
        )

    without_ci, with_ci = collected
    for label, node_ids in (("without CI", without_ci), ("with CI", with_ci)):
        agentic = {n for n in node_ids if AGENTIC_DIR_FRAGMENT in n}
        assert agentic, (
            f"no case under {AGENTIC_DIR_FRAGMENT} was collected {label}, although "
            "strands and pyarrow are both importable here. Those tests are what "
            "covers extraction/agentic_idp.py; if they are gone rather than skipped, "
            "the coverage ratchet is the only thing that will notice, and it will "
            "report it as a regression in the module instead."
        )
