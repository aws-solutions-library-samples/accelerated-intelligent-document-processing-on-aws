# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for ``make premerge`` (``scripts/premerge.py``, #2115).

The property that matters is that the local run *is* the CI gate set: every entry
of ``SHARED_GATES`` becomes a command, nothing outside it does, and each command
names something that exists. The runner itself is checked for the one behaviour a
pre-merge battery cannot get wrong quietly -- carrying on past a failure and still
exiting non-zero.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tests"))

import premerge  # noqa: E402
import test_ci_gate_parity  # noqa: E402

MAKE_TARGET = re.compile(r"^([A-Za-z0-9][\w.-]*)\s*:(?!=)", re.MULTILINE)


def _targets(makefile: Path) -> set[str]:
    text = makefile.read_text(encoding="utf-8")
    found = set(MAKE_TARGET.findall(text))
    for include in (REPO_ROOT / "make").glob("*.mk"):
        if makefile.parent == REPO_ROOT:
            found |= set(MAKE_TARGET.findall(include.read_text(encoding="utf-8")))
    return found


@pytest.mark.unit
def test_the_gate_list_is_read_from_the_parity_module() -> None:
    """The ast reader returns the same list the parity test asserts both CIs run."""
    assert premerge.shared_gates() == test_ci_gate_parity.SHARED_GATES


@pytest.mark.unit
def test_every_shared_gate_has_a_local_plan() -> None:
    plan = premerge.full_plan(premerge.shared_gates())
    assert [gate for gate, _ in plan] == test_ci_gate_parity.SHARED_GATES
    assert all(steps for _, steps in plan)


@pytest.mark.unit
def test_no_local_invocation_outlives_its_gate() -> None:
    stale = set(premerge.LOCAL_INVOCATIONS) - set(test_ci_gate_parity.SHARED_GATES)
    assert not stale, f"LOCAL_INVOCATIONS names gates CI no longer runs: {stale}"


@pytest.mark.unit
def test_a_gate_of_an_unknown_shape_is_refused_rather_than_skipped() -> None:
    with pytest.raises(ValueError, match="LOCAL_INVOCATIONS"):
        premerge.plan_for("docker run something")


@pytest.mark.unit
def test_every_step_names_something_that_exists() -> None:
    """A make target, script or working directory that has gone is a broken plan."""
    root_targets = _targets(REPO_ROOT / "Makefile")
    for gate, steps in premerge.full_plan(premerge.shared_gates()):
        for step in steps:
            cwd = REPO_ROOT / step.cwd
            assert cwd.is_dir(), (gate, step.cwd)
            argv = list(step.argv)
            if argv[0] == "make":
                directory = REPO_ROOT
                if "-C" in argv:
                    directory = REPO_ROOT / argv[argv.index("-C") + 1]
                    del argv[argv.index("-C") : argv.index("-C") + 2]
                targets = (
                    root_targets
                    if directory == REPO_ROOT
                    else _targets(directory / "Makefile")
                )
                target = next(word for word in argv[1:] if "=" not in word)
                assert target in targets, (gate, target)
            elif argv[0] == sys.executable:
                assert (REPO_ROOT / argv[1]).is_file(), (gate, argv[1])


@pytest.mark.unit
def test_the_make_target_runs_the_script() -> None:
    text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    recipe = re.search(r"^premerge:.*\n\t(.+)$", text, re.MULTILINE)
    assert recipe is not None, "no premerge target in the Makefile"
    assert "scripts/premerge.py" in recipe.group(1)


@pytest.mark.unit
def test_list_prints_every_gate_and_runs_nothing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("--list ran a command")

    monkeypatch.setattr(premerge.subprocess, "run", _refuse)
    assert premerge.main(["--list"]) == 0
    out = capsys.readouterr().out
    for gate in test_ci_gate_parity.SHARED_GATES:
        assert f"{gate}\n" in out


@pytest.mark.unit
def test_the_runner_carries_on_past_a_failure_and_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marker = tmp_path / "ran"
    plan = [
        ("first", (premerge.Step(("false",)),)),
        (
            "second",
            (premerge.Step((sys.executable, "-c", f"open({str(marker)!r}, 'w')")),),
        ),
    ]
    assert premerge.run(plan, root=tmp_path) == 1
    assert marker.exists(), "the gate after a failure did not run"
    out = capsys.readouterr().out
    assert "FAIL (exit 1)" in out
    assert "1 of 2 CI gates failed" in out


@pytest.mark.unit
def test_a_gate_stops_at_its_own_first_failing_step(tmp_path: Path) -> None:
    """srt-setup failing must not run srt-scan against nothing."""
    marker = tmp_path / "ran"
    steps = (
        premerge.Step(("false",)),
        premerge.Step((sys.executable, "-c", f"open({str(marker)!r}, 'w')")),
    )
    assert premerge.run_gate(steps, root=tmp_path) == 1
    assert not marker.exists()


@pytest.mark.unit
def test_the_srt_scan_is_set_up_first_and_run_as_a_gate() -> None:
    """Without CI set the scan reports findings and exits 0 (scripts/srt/run.py)."""
    steps = premerge.plan_for("make srt-scan")
    assert [step.argv for step in steps] == [
        ("make", "srt-setup"),
        ("make", "srt-scan"),
    ]
    assert all(step.env.get("CI") == "1" for step in steps)


@pytest.mark.unit
def test_the_library_suite_does_not_reinstall_into_a_shared_interpreter() -> None:
    """test-cicd reinstalls idp_common unless SKIP_INSTALL=1 (#1094)."""
    (step,) = premerge.plan_for("make test-cicd")
    assert "SKIP_INSTALL=1" in step.argv


@pytest.mark.unit
def test_a_steps_environment_reaches_its_command(tmp_path: Path) -> None:
    check = (
        "import os, sys; sys.exit(0 if os.environ.get('PREMERGE_PROBE') == 'on' else 3)"
    )
    steps = (
        premerge.Step((sys.executable, "-c", check), env={"PREMERGE_PROBE": "on"}),
    )
    assert premerge.run_gate(steps, root=tmp_path) == 0
    bare = (premerge.Step((sys.executable, "-c", check)),)
    assert premerge.run_gate(bare, root=tmp_path) == 3


@pytest.mark.unit
def test_the_runner_passes_when_every_gate_passes(tmp_path: Path) -> None:
    plan = [("only", (premerge.Step(("true",), env={"X": "1"}),))]
    assert premerge.run(plan, root=tmp_path) == 0


@pytest.mark.unit
def test_a_missing_program_is_a_failure_not_a_crash(tmp_path: Path) -> None:
    steps = (premerge.Step(("definitely-not-a-program-2115",)),)
    assert premerge.run_gate(steps, root=tmp_path) == 127
