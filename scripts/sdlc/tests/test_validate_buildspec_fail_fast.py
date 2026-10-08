"""The buildspec gate refuses a loop that cannot report its own failure (#1310).

Two things are pinned here, and they are different claims. The first is that the
check *works*: a loop without errexit is an error, errexit before the loop clears
it, and errexit after the loop does not. The second is that the check is *reached*
-- that `make validate-buildspec` discovers every buildspec in the tree, not the
one the old `patterns/*/buildspec.yml` glob happened to match. A check nothing is
run against is the shape #1310 survived in: three of the four buildspecs carried
the same defect and the gate never opened them.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
VALIDATOR = REPO_ROOT / "scripts" / "sdlc" / "validate_buildspec.py"

# The name shape the Makefile's discovery matches, kept here independently so a
# change to one side has to be made on the other deliberately.
BUILDSPEC_NAME = re.compile(r"(?:^|/)buildspec[^/]*\.ya?ml$")


def _run(tmp_path: Path, buildspec: dict) -> subprocess.CompletedProcess:
    path = tmp_path / "buildspec.yml"
    path.write_text(yaml.safe_dump(buildspec, default_flow_style=False))
    return subprocess.run(
        [sys.executable, str(VALIDATOR), str(path)],
        capture_output=True,
        text=True,
    )


def _spec(command: str) -> dict:
    return {"version": 0.2, "phases": {"build": {"commands": [command]}}}


LOOP_BODY = "  docker buildx build --push -t $REPO:$f .\ndone\n"


def test_a_loop_without_errexit_is_an_error(tmp_path: Path):
    result = _run(tmp_path, _spec("for f in a b c; do\n" + LOOP_BODY))
    assert result.returncode == 1, result.stdout
    assert "does not enable errexit" in result.stdout
    assert "#1310" in result.stdout


def test_errexit_before_the_loop_clears_it(tmp_path: Path):
    result = _run(tmp_path, _spec("set -e\nfor f in a b c; do\n" + LOOP_BODY))
    assert result.returncode == 0, result.stdout
    assert "does not enable errexit" not in result.stdout


@pytest.mark.parametrize("setline", ["set -eu", "set -euo pipefail", "set -o errexit"])
def test_the_other_spellings_of_errexit_count(tmp_path: Path, setline: str):
    result = _run(tmp_path, _spec(f"{setline}\nfor f in a b; do\n" + LOOP_BODY))
    assert result.returncode == 0, result.stdout


def test_pipefail_alone_is_not_errexit(tmp_path: Path):
    """`-o pipefail` changes how a *pipeline's* status is computed and does not
    make the shell exit on one, so it must not satisfy this check."""
    result = _run(tmp_path, _spec("set -o pipefail\nfor f in a b; do\n" + LOOP_BODY))
    assert result.returncode == 1, result.stdout
    assert "does not enable errexit" in result.stdout


def test_errexit_after_the_loop_does_not_count(tmp_path: Path):
    """Ordering is the whole point: a `set -e` the loop has already run past
    protects nothing, and a check that only asked whether the string appears
    anywhere in the block would pass this."""
    result = _run(tmp_path, _spec("for f in a b; do\n" + LOOP_BODY + "set -e\n"))
    assert result.returncode == 1, result.stdout


def test_a_single_line_loop_is_left_alone(tmp_path: Path):
    """CodeBuild judges a single-line command on its own exit status, so a
    one-line loop aborts the phase without errexit and needs no flag."""
    result = _run(tmp_path, _spec("for f in a b; do echo $f; done"))
    assert result.returncode == 0, result.stdout


def test_the_word_for_in_prose_is_not_a_loop(tmp_path: Path):
    """The pattern anchors on statement position. An echo that merely contains
    the word would otherwise demand errexit of every buildspec in the tree."""
    result = _run(
        tmp_path,
        _spec('echo "building images for every function"\necho "second line"\n'),
    )
    assert result.returncode == 0, result.stdout


def _discovered_buildspecs() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return sorted(p for p in out if BUILDSPEC_NAME.search(p))


def test_every_tracked_buildspec_is_handed_to_the_gate():
    """Universe closure: the Makefile's discovery must reach every buildspec file
    in the tree. This is the half that was broken -- `patterns/*/buildspec.yml`
    matched one of four."""
    discovered = _discovered_buildspecs()
    assert discovered, "no buildspec files found; the gate would pass vacuously"

    makefile = (REPO_ROOT / "Makefile").read_text()
    assert "BUILDSPEC_FILES = $(shell git ls-files" in makefile, (
        "validate-buildspec no longer derives its file set from git ls-files; a "
        "hardcoded glob is how three of four buildspecs went unread (#1310)"
    )

    result = subprocess.run(
        ["make", "validate-buildspec"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for path in discovered:
        assert path in result.stdout, (
            f"{path} is a tracked buildspec that `make validate-buildspec` did not read"
        )


def test_every_tracked_buildspec_actually_passes_the_new_check():
    """Non-vacuity in the other direction: the check is only worth having if the
    tree currently satisfies it, so a regression in any buildspec fails here with
    the file named rather than inside a make target's output."""
    failures = []
    for path in _discovered_buildspecs():
        result = subprocess.run(
            [sys.executable, str(VALIDATOR), path],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            failures.append(f"{path}:\n{result.stdout}")
    assert not failures, "\n".join(failures)
