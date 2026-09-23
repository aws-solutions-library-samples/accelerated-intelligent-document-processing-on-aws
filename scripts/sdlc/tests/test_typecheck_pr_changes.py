# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``typecheck_pr_changes.py`` cannot report success without having checked something.

This script had no tests at all, and three separate routes to a false pass:

1. ``return result.returncode if result.returncode in [0, 1] else 0`` — an exit code
   it did not recognise became a pass. A nonexistent path exits 4 and a malformed
   config exits 3, so both read as "clean".
2. The summary was read out of prose. When the line was absent — which is what a
   fatal config error produces — the parse fell through to that same ``else 0``.
3. Nothing compared what ``basedpyright`` analysed against what was selected.
   ``basedpyright`` prints ``0 errors, 0 warnings, 0 notes`` and exits 0 when its
   file set resolves to nothing, so a run that analysed **nothing** is textually
   identical to a clean run.

Each of the three has a test below that fails if the route reopens. The first two
are driven through :func:`interpret_result` with fabricated process output; the third
is driven twice — fabricated, and against the real ``basedpyright`` in the one
configuration that genuinely analyses nothing.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts/sdlc/typecheck_pr_changes.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("typecheck_pr_changes", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["typecheck_pr_changes"] = module
    spec.loader.exec_module(module)
    return module


def _report(analysed: int, errors: int = 0, warnings: int = 0, diagnostics=None) -> str:
    return json.dumps(
        {
            "version": "1.32.1",
            "generalDiagnostics": diagnostics or [],
            "summary": {
                "filesAnalyzed": analysed,
                "errorCount": errors,
                "warningCount": warnings,
                "informationCount": 0,
                "timeInSec": 0.1,
            },
        }
    )


# --------------------------------------------------------------------------- #
# The happy paths, so the failure tests below are not passing vacuously.
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_clean_run_over_the_selected_files_passes(mod) -> None:
    code, lines = mod.interpret_result(["a.py", "b.py"], 0, _report(analysed=2), "")
    assert code == 0, lines
    assert "2 files analysed" in "\n".join(lines)


@pytest.mark.unit
def test_errors_in_the_selected_files_fail(mod) -> None:
    code, lines = mod.interpret_result(
        ["a.py"],
        1,
        _report(
            analysed=1,
            errors=1,
            diagnostics=[
                {
                    "file": "a.py",
                    "severity": "error",
                    "message": 'Type "str" is not assignable to "int"',
                    "rule": "reportReturnType",
                    "range": {"start": {"line": 4, "character": 11}},
                }
            ],
        ),
        "",
    )
    assert code == 1
    rendered = "\n".join(lines)
    assert "a.py:5:12" in rendered
    assert "reportReturnType" in rendered


@pytest.mark.unit
def test_warnings_alone_do_not_fail(mod) -> None:
    """The repo's type gate is error-level; 91 warnings exist and are advisory."""
    code, _ = mod.interpret_result(["a.py"], 0, _report(analysed=1, warnings=3), "")
    assert code == 0


# --------------------------------------------------------------------------- #
# Fail-open route 1: an exit code the script does not recognise.
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize("returncode", [2, 3, 4, 127, -9])
def test_an_uninterpretable_exit_code_fails_rather_than_passes(mod, returncode) -> None:
    """``else 0`` is closed: only 0 and 1 mean "the check ran".

    3 is a config basedpyright could not parse and 4 a path that does not exist.
    Both used to be reported as success, and a gate that passes when the checker
    never ran is worse than no gate, because it reads as evidence.
    """
    code, lines = mod.interpret_result(
        ["a.py"], returncode, "", "Config file could not be parsed."
    )
    assert code == 1, f"exit {returncode} was treated as a pass"
    assert "does not mean" in "\n".join(lines)


@pytest.mark.unit
def test_only_zero_and_one_are_interpretable(mod) -> None:
    """Non-vacuity guard for the parametrisation above.

    If this tuple is ever widened, the test above starts passing for the codes it
    was written to reject, silently.
    """
    assert mod.INTERPRETABLE_EXIT_CODES == (0, 1)


# --------------------------------------------------------------------------- #
# Fail-open route 2: output the script cannot parse.
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "0 errors, 0 warnings, 0 notes\n",  # the prose form, no JSON
        "not json at all",
        '{"summary": {}}',  # JSON, but no counts
        '{"summary": {"filesAnalyzed": "many"}}',  # counts of the wrong type
    ],
    ids=["empty", "prose-summary", "garbage", "no-counts", "wrong-type"],
)
def test_unparseable_output_fails(mod, stdout) -> None:
    """An unreadable verdict is a failure.

    ``0 errors, 0 warnings, 0 notes`` as *prose* is included deliberately: that is
    exactly what the old regex accepted, and accepting it is how a fatal config
    error reported a clean tree.
    """
    code, lines = mod.interpret_result(["a.py"], 0, stdout, "")
    assert code == 1, f"{stdout!r} was treated as a pass"
    assert "Could not read basedpyright's JSON report" in "\n".join(lines)


# --------------------------------------------------------------------------- #
# Fail-open route 3: a run that analysed nothing.
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_analysing_nothing_fails_even_though_it_reports_zero_errors(mod) -> None:
    """The reconciliation. Without it this exact payload is a clean pass."""
    code, lines = mod.interpret_result(["a.py", "b.py"], 0, _report(analysed=0), "")
    assert code == 1
    rendered = "\n".join(lines)
    assert "analysed 0 file(s) but 2 were selected" in rendered


@pytest.mark.unit
def test_analysing_fewer_than_selected_fails(mod) -> None:
    """A partial run is a partial verdict, so it does not clear the files selected."""
    code, _ = mod.interpret_result(["a.py", "b.py", "c.py"], 0, _report(analysed=2), "")
    assert code == 1


@pytest.mark.unit
def test_a_mismatch_fails_even_when_errors_are_reported_and_fixed(mod) -> None:
    """Order of checks: reconciliation applies to error-free and error runs alike."""
    code, _ = mod.interpret_result(
        ["a.py", "b.py"], 1, _report(analysed=1, errors=1), ""
    )
    assert code == 1


# --------------------------------------------------------------------------- #
# The same three routes, against the real checker.
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.skipif(
    shutil.which("basedpyright") is None,
    reason="basedpyright is an npm devDependency, not installed by make setup",
)
def test_real_basedpyright_reports_zero_errors_for_a_run_that_analysed_nothing(
    tmp_path,
) -> None:
    """The behaviour the reconciliation exists for, measured rather than assumed.

    A config whose ``include`` cannot be resolved makes ``basedpyright`` emit
    ``filesAnalyzed: 0`` with ``errorCount: 0``. In its text output that is the
    byte-for-byte summary of a clean tree. This test asserts the *tool's* behaviour;
    the test above asserts that this script no longer accepts it.
    """
    config = tmp_path / "pyrightconfig.json"
    config.write_text(json.dumps({"include": [str(tmp_path / "no_such_tree")]}))

    result = subprocess.run(
        ["basedpyright", "--outputjson", "--project", str(config)],
        capture_output=True,
        text=True,
        check=False,
    )
    summary = json.loads(result.stdout)["summary"]
    assert summary["filesAnalyzed"] == 0
    assert summary["errorCount"] == 0, (
        "basedpyright no longer reports a resolved-to-nothing run as error-free. "
        "That is a better tool, but check interpret_result's reconciliation is "
        "still reachable before relaxing it."
    )


@pytest.mark.unit
@pytest.mark.skipif(
    shutil.which("basedpyright") is None,
    reason="basedpyright is an npm devDependency, not installed by make setup",
)
def test_end_to_end_over_a_real_file_with_a_real_error(tmp_path, mod) -> None:
    """Drive the real checker through ``run_type_check`` and require a failure.

    Also covers the argument-passing change: the files are given to
    ``basedpyright`` as command-line arguments rather than through a generated
    temporary config, so this asserts that ``filesAnalyzed`` comes back equal to
    the number passed.

    The defect used is an undefined name, not a wrong return type. Which defects
    are *errors* here is a property of ``pyrightconfig.json``, and it sets
    ``reportReturnType`` to ``warning`` — so a bad return annotation does not fail
    this gate, while ``reportUndefinedVariable`` is ``error`` and does.
    """
    target = tmp_path / "broken.py"
    target.write_text("def f() -> int:\n    return no_such_name\n")

    result = subprocess.run(
        ["basedpyright", "--outputjson", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    code, lines = mod.interpret_result(
        [str(target)], result.returncode, result.stdout, result.stderr
    )
    assert code == 1, lines
    assert "1 files analysed" in "\n".join(lines)


@pytest.mark.unit
@pytest.mark.skipif(
    shutil.which("basedpyright") is None,
    reason="basedpyright is an npm devDependency, not installed by make setup",
)
def test_end_to_end_over_a_real_clean_file(tmp_path, mod) -> None:
    """The counterpart, so the test above is not passing for an unrelated reason."""
    target = tmp_path / "clean.py"
    target.write_text("def f() -> int:\n    return 1\n")

    result = subprocess.run(
        ["basedpyright", "--outputjson", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    code, lines = mod.interpret_result(
        [str(target)], result.returncode, result.stdout, result.stderr
    )
    assert code == 0, lines


@pytest.mark.unit
@pytest.mark.skipif(
    shutil.which("basedpyright") is None,
    reason="basedpyright is an npm devDependency, not installed by make setup",
)
def test_a_nonexistent_path_is_not_a_pass(tmp_path, mod) -> None:
    """The script's own ``Path(f).exists()`` filter is not the only thing stopping this.

    Two guards cover it now: the selection filter, and an exit code the
    interpretation does not recognise.
    """
    missing = tmp_path / "gone.py"
    result = subprocess.run(
        ["basedpyright", "--outputjson", str(missing)],
        capture_output=True,
        text=True,
        check=False,
    )
    code, _ = mod.interpret_result(
        [str(missing)], result.returncode, result.stdout, result.stderr
    )
    assert code == 1


# --------------------------------------------------------------------------- #
# It is documented as a developer command, not a gate.
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_script_does_not_present_itself_as_the_gate() -> None:
    """A file-scoped type check that reads as the gate is how this got into CI.

    The gate is ``make typecheck``. This asserts the script's own docstring says
    so, because the previous docstring's "Ensuring new code doesn't introduce type
    errors" is what it was wired into CI on the strength of.
    """
    text = SCRIPT.read_text()
    docstring = text.split('"""')[1]
    assert "not a CI gate" in docstring
    assert "make typecheck" in docstring


@pytest.mark.unit
def test_no_temporary_pyright_config_is_written(mod) -> None:
    """The narrowing is by argument, so no config copy can drift from the real one.

    Read the *code*, not the whole file: the module docstring names the old
    temporary-config filename while explaining why it is gone, and a plain
    substring search over the source would match that explanation.
    """
    tree = ast.parse(SCRIPT.read_text())
    code = ast.unparse(
        ast.Module(
            body=[n for n in tree.body if not _is_docstring_expr(n)], type_ignores=[]
        )
    )
    # A substring search for the filename is the wrong instrument — the error
    # messages legitimately name pyrightconfig.json when telling the operator why
    # a file may have gone unanalysed. Assert the two behaviours instead.
    assert "--project" not in code, (
        "basedpyright is being pointed at a config file. The file set must be "
        "narrowed by command-line argument so that pyrightconfig.json's other "
        "settings stay in force rather than being copied into a generated config."
    )
    for writer in ("write_text(", "open(", "json.dump("):
        assert writer not in code, f"the script writes a file ({writer})"
    assert not hasattr(mod, "create_temp_config")


def _is_docstring_expr(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)


@pytest.mark.unit
def test_neither_ci_config_runs_this_script() -> None:
    """Its exclusion from CI is the decision; assert it rather than remember it.

    Kept here rather than only in ``test_ci_gate_parity.py`` because this is the
    property of *this script* that everything above depends on: it is allowed to be
    narrow precisely because nothing merges on its verdict.
    """
    for config in (
        REPO_ROOT / ".gitlab-ci.yml",
        REPO_ROOT / ".github/workflows/developer-tests.yml",
        REPO_ROOT / ".github/workflows/security-checks.yml",
    ):
        text = config.read_text()
        for invocation in ("make typecheck-pr", "typecheck_pr_changes.py"):
            offending = [
                line
                for line in text.splitlines()
                if invocation in line and not line.strip().startswith("#")
            ]
            assert not offending, (
                f"{config.name} invokes {invocation!r}: {offending}. This is a "
                f"file-scoped check and cannot see a break in a file the diff did "
                f"not touch. The CI type gate is `make typecheck`."
            )
