# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The `idp_common` suite is distributed across cores by default, exactly once.

**This is primarily about the local run**, which is where this suite is exercised most
often and where the cost was being paid in full. `pytest-xdist` has been a declared
`[test]` dependency since it was added "For parallel test execution", but no Makefile
target ever passed `-n`, so every local `make test` and `make test-cicd` ran serially.

Measured, same tree, full suite with coverage on a 16-core host:

    serial    8306 passed, TOTAL 79%   in 584s
    -n auto   8306 passed, TOTAL 79%   in  94s     (6.2x)

Identical pass count and identical coverage total, which is also a statement about the
suite: nothing in it depends on execution order, or distributing it would have changed a
result.

Making it a Makefile default introduces one failure mode worth pinning. GitLab used to
pass `-n auto` itself via `PYTEST_ARGS`; with the default in place that would put `-n` on
the command line **twice**, and pytest answers that by collecting nothing and exiting 5.
The job fails, so no false green ships — but the suite did not run, and the output says
only "9 warnings" with no test count, which is not an obvious diagnosis. The override has
been removed from the CI config and this module keeps it from coming back.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_MAKEFILE = REPO_ROOT / "lib" / "idp_common_pkg" / "Makefile"

#: Every CI configuration that invokes the package's test target.
CI_CONFIGS = (
    REPO_ROOT / ".gitlab-ci.yml",
    REPO_ROOT / ".github" / "workflows" / "developer-tests.yml",
)


def _makefile() -> str:
    return PACKAGE_MAKEFILE.read_text(encoding="utf-8")


@pytest.mark.unit
def test_the_parallel_flag_is_a_makefile_default():
    """Defined in the Makefile, so every caller inherits it rather than opting in."""
    assert re.search(r"^PYTEST_PARALLEL \?= -n auto$", _makefile(), re.M), (
        "PYTEST_PARALLEL is not defaulted to `-n auto` in lib/idp_common_pkg/Makefile"
    )


@pytest.mark.unit
def test_the_default_is_overridable_rather_than_hardcoded():
    # `?=` not `=`: xdist interleaves worker output and breaks `-s`, `--pdb` and live
    # logging, so a debugging run has to be able to turn it off with `PYTEST_PARALLEL=`.
    assert "PYTEST_PARALLEL ?=" in _makefile()
    assert "PYTEST_PARALLEL =" not in _makefile().replace("PYTEST_PARALLEL ?=", "")


@pytest.mark.unit
@pytest.mark.parametrize("target", ["test-unit", "test-unit-cicd"])
def test_both_unit_targets_use_it(target):
    """The local target and the CI target, so a local run and CI agree on cost."""
    body = re.search(rf"^{re.escape(target)}:\n((?:\t.*\n|#.*\n)+)", _makefile(), re.M)
    assert body, f"could not find the {target} recipe"
    assert "$(PYTEST_PARALLEL)" in body.group(1), (
        f"{target} does not pass $(PYTEST_PARALLEL), so it still runs serially"
    )


@pytest.mark.unit
def test_the_integration_target_is_deliberately_serial():
    """Integration tests share real AWS resources, so workers would race on them.

    Asserted rather than left implicit: adding `-n` here would turn a slow suite into a
    flaky one, and flakiness against live resources is expensive to diagnose.
    """
    body = re.search(r"^test-integration:\n((?:\t.*\n)+)", _makefile(), re.M)
    assert body, "could not find the test-integration recipe"
    assert "PYTEST_PARALLEL" not in body.group(1)
    assert " -n " not in body.group(1)


@pytest.mark.unit
@pytest.mark.parametrize("config", CI_CONFIGS, ids=lambda p: p.name)
def test_no_ci_config_passes_n_on_top_of_the_default(config):
    """Two `-n` on one command line makes pytest collect nothing and exit 5.

    Measured: `pytest -n auto -n auto <file>` reports a warnings summary, no test count,
    and exit 5. The job fails, so nothing false ships — but the output does not say why,
    and the suite that was supposed to run did not.
    """
    if not config.is_file():
        pytest.skip(f"{config.name} is not present in this tree")
    text = config.read_text(encoding="utf-8")
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        if "test-cicd -C lib/idp_common_pkg" not in line:
            continue
        assert "-n auto" not in line and "PYTEST_ARGS" not in line, (
            f"{config.name}:{line_number} passes pytest args to the package test "
            f"target; `-n` now comes from the Makefile's PYTEST_PARALLEL, and passing "
            f"it twice collects zero tests:\n    {line.strip()}"
        )


@pytest.mark.unit
def test_xdist_is_a_declared_test_dependency():
    """The default would break a fresh environment if the dependency were implicit.

    It is declared today, and was before any target used it -- the gap was never a
    missing dependency, only an unused one.
    """
    pyproject = (REPO_ROOT / "lib" / "idp_common_pkg" / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert "pytest-xdist" in pyproject, (
        "pytest-xdist is not declared, so `-n auto` would fail on a clean install"
    )
