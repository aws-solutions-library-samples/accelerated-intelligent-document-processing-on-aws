# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Assert that every test directory in this repository is actually run by CI.

Two recipes run this repository's offline suites on a pull request, and between them
they are the whole of it: ``make test-packages-cicd`` at the repository root, and
``make test-cicd`` in ``lib/idp_common_pkg``. Both are invoked by
``.github/workflows/developer-tests.yml`` and by ``.gitlab-ci.yml``. Both enumerate
paths by hand, so a test directory that nobody adds to one of them is never run by
CI — silently, and with a green pipeline.

``scripts/tests/test_ci_gate_parity.py`` cannot see this: it compares gate *names*
between the two CI configs, so a suite missing from **both** sides is invisible to
it by construction. ``scripts/run_all_tests.py`` does discover every test root, but
it backs ``make test``, which runs in neither CI — so its registry proves a suite is
*runnable*, not that it is *run*.

Both sides are DERIVED, never listed here. The universe comes from ``git ls-files``
(via ``repo_files.tracked_paths``) and the covered set comes from parsing the two
recipes. A guard that enumerated the directories it knows about would reproduce the
defect it exists to prevent — and one that walked the filesystem instead of asking
git would report findings against build output and sibling agent worktrees, which is
its own recurring defect here (see ``repo_files``' module docstring).

Deliberately excluded directories are taken from ``run_all_tests.QUARANTINE``, which
already carries a written reason for each one, so there is exactly one place to
record "this suite does not run in the shared gate" instead of two. There is no
exemption list in this file, by design: a root is either reachable from a
CI-invoked target or it is quarantined with a reason.

**Scope, and how it got here.** This check began as ``src/lambda/``-only, and said so
— the same argument applied to ``patterns/unified/tests`` and the
``nested/api-resolvers`` Lambdas, and issue #980 tracked generalising it. Scoping the
fix to one subtree while writing down that the class was wider left 22 registered
roots holding 506 tests outside both CIs, including a resolver suite added by a fix
whose own commit message said those tests were "what would have caught it", and the
25 tests covering the download allow-list, script-type download forcing and
fail-closed refusals of ``get_file_contents_resolver``. The universe is now the whole
tree.

One scope limit remains, and it is narrower than the old one. This asserts DIRECTORY
coverage: a recipe line that names an individual file inside a covered directory
(``cd src/lambda/queue_sender && pytest test_index.py``) still counts as covering it,
so a second test module added beside an enumerated one is not caught. That directory
holds only the file it names today, so there is no live gap; closing the file-level
case would mean requiring bare-directory invocations throughout.
"""

from __future__ import annotations

import importlib.util
import shlex
from pathlib import Path

import pytest
from repo_files import tracked_paths

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Every recipe a pull request runs that invokes pytest, as (Makefile, target).
#: Both are in ``test_ci_gate_parity.SHARED_GATES``, which is what makes "runs on a
#: pull request" true of them rather than assumed.
CI_RECIPES: tuple[tuple[str, str], ...] = (
    ("Makefile", "test-packages-cicd"),
    ("lib/idp_common_pkg/Makefile", "test-unit-cicd"),
)

#: The Make variable every gated pytest invocation goes through.
PYTEST_VAR = "PYTEST_HERMETIC"

pytestmark = pytest.mark.unit


def _run_all_tests_module():
    """Import scripts/run_all_tests.py by path (it is a script, not a package)."""
    path = REPO_ROOT / "scripts" / "run_all_tests.py"
    spec = importlib.util.spec_from_file_location("_run_all_tests_for_ci_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recipe_body(makefile: Path, target: str) -> list[str]:
    """The tab-indented lines of one recipe, with ``\\`` continuations joined."""
    lines = makefile.read_text(encoding="utf-8").splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith(target + ":")]
    assert len(starts) == 1, (
        f"expected exactly one '{target}:' rule in "
        f"{makefile.relative_to(REPO_ROOT)}, found {len(starts)}"
    )
    body: list[str] = []
    pending = ""
    for ln in lines[starts[0] + 1 :]:
        if not ln.startswith("\t"):
            if ln.strip() == "":
                continue
            break
        fragment = ln.lstrip("\t")
        pending = f"{pending} {fragment.strip()}" if pending else fragment
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        body.append(pending)
        pending = ""
    if pending:
        body.append(pending)
    assert body, f"parsed an empty recipe body for {target}"
    return body


def _runnable(lines: list[str]) -> list[str]:
    """Recipe lines that run something.

    ``@echo`` progress messages and ``@#`` comments are dropped first: a path named
    in a message or a comment must not be able to satisfy this test.
    """
    return [
        ln
        for ln in lines
        if not ln.lstrip("@").startswith("#") and not ln.startswith("@echo")
    ]


def _covered_dirs() -> set[str]:
    """Repo-relative directories the CI recipes actually run pytest against.

    A pytest target that names a file is recorded as its parent directory, which is
    what makes the directory-level assertion below correct rather than strict.
    """
    covered: set[str] = set()
    for makefile_rel, target in CI_RECIPES:
        makefile = REPO_ROOT / makefile_rel
        workdir_root = makefile.parent.relative_to(REPO_ROOT).as_posix()
        for line in _runnable(_recipe_body(makefile, target)):
            if f"$({PYTEST_VAR})" not in line:
                continue
            workdir = workdir_root
            command = line
            if command.startswith("cd "):
                head, _, tail = command.partition("&&")
                relative = head[len("cd ") :].strip()
                workdir = f"{workdir_root}/{relative}" if workdir_root else relative
                command = tail.strip()
            _, _, argument_text = command.partition(f"$({PYTEST_VAR})")
            targets = _target_paths(shlex.split(argument_text))
            base = Path(workdir.rstrip("/")) if workdir not in ("", ".") else Path()
            if not targets:
                covered.add(base.as_posix() or ".")
                continue
            for target_path in targets:
                resolved = (base / target_path).as_posix().rstrip("/")
                if (REPO_ROOT / resolved).is_file():
                    resolved = Path(resolved).parent.as_posix()
                covered.add(resolved)
    return covered


#: pytest options whose VALUE is a separate argument, so the value is not a path.
_OPTIONS_TAKING_A_VALUE = frozenset({"-m", "-k", "-p", "-n", "--deselect", "--ignore"})


def _target_paths(arguments: list[str]) -> list[str]:
    """The test paths in a pytest argument list.

    Option values are excluded, and so is anything still holding an unexpanded Make
    variable — ``$(PYTEST_ARGS)`` is a pass-through for CI's ``-n auto`` and is not a
    path, but it does not start with ``-`` and would otherwise be read as one.
    """
    paths: list[str] = []
    skip_next = False
    for argument in arguments:
        if skip_next:
            skip_next = False
            continue
        if argument.startswith("-"):
            skip_next = argument in _OPTIONS_TAKING_A_VALUE
            continue
        if "$(" in argument:
            continue
        paths.append(argument)
    return paths


def _test_dirs(root: Path) -> set[str]:
    """Every directory under ``root`` holding a ``test_*.py`` that git accounts for.

    Derived with ``tracked_paths`` rather than a filesystem walk. ``run_all_tests``'s
    two prune lists are reused rather than restated, so the definition of "not a
    source test root" lives in one place — and so the two sides of this gate cannot
    disagree about what a test directory is.
    """
    module = _run_all_tests_module()
    dirs: set[str] = set()
    # ``*.py`` and then filter on the name, rather than asking for ``test_*.py``.
    # ``tracked_paths`` matches its patterns as a git pathspec against the whole
    # repo-relative path on the git route and as an ``fnmatch`` against the bare file
    # name on the fallback walk, and no single glob means the same thing to both:
    # ``test_*.py`` as a pathspec matches only the repository root, and
    # ``*/test_*.py`` as an fnmatch matches nothing at all.
    for path in tracked_paths(root, "*.py"):
        if not path.name.startswith("test_"):
            continue
        relative = path.relative_to(root).as_posix()
        if any(marker in f"/{relative}" for marker in module.PRUNE_DIR_MARKERS):
            continue
        if path.name.endswith(module.PRUNE_FILE_SUFFIXES):
            continue
        dirs.add(Path(relative).parent.as_posix())
    return dirs


def _covered_by(directory: str, candidates: set[str]) -> bool:
    """True if `directory` equals, or sits under, any path in `candidates`."""
    return any(
        directory == candidate or directory.startswith(candidate.rstrip("/") + "/")
        for candidate in candidates
    )


def _uncovered(root: Path, covered: set[str], quarantined: set[str]) -> list[str]:
    """The detector itself, parameterised on its inputs so a probe can drive it."""
    return sorted(
        directory
        for directory in _test_dirs(root)
        if not _covered_by(directory, quarantined)
        and not _covered_by(directory, covered)
    )


def test_derivation_is_not_vacuous():
    """A guard whose two derived sets are empty passes for the wrong reason.

    Neither side is checked against a fixed list — that would be the defect this file
    exists to prevent. Instead: the git listing must find directories that provably
    hold tests, and every path the recipe parse yields must exist on disk. A broken
    listing, a broken parse, or a recipe naming a path that no longer exists all fail
    here rather than quietly turning the assertion below into a no-op.
    """
    discovered = _test_dirs(REPO_ROOT)
    for expected in ("src/lambda/workflow_tracker", "benchmarks/tests"):
        assert expected in discovered, (
            f"git lists no test_*.py under {expected}, which does hold one; discovery "
            f"is broken. Found {len(discovered)} directories."
        )

    covered = _covered_dirs()
    assert len(covered) > 20, (
        "the recipe parse found only "
        f"{sorted(covered)} — far fewer directories than the recipes name, so the "
        "parse is broken and the coverage assertion below would pass vacuously."
    )
    stale = sorted(path for path in covered if not (REPO_ROOT / path).is_dir())
    assert not stale, (
        "the CI recipes run pytest against paths that do not exist:\n  "
        + "\n  ".join(stale)
    )


def test_the_detector_reports_an_uncovered_directory(tmp_path: Path):
    """Can it fail? Drive the real detector against a synthetic tree.

    Deleting the body of ``_uncovered`` must not leave this suite green. The probe is
    written into ``tmp_path`` and never into the repository: a probe file under the
    checkout would be picked up by every other tree-wide gate in this directory for as
    long as it existed, which is a cost this repository has already paid once.

    Both directions are asserted. An uncovered directory must be reported, and one
    declared covered must not be — on its own the first half is satisfied by a
    detector that reports everything.
    """
    (tmp_path / "svc" / "tests").mkdir(parents=True)
    (tmp_path / "svc" / "tests" / "test_probe.py").write_text(
        "def test_x():\n    pass\n"
    )
    (tmp_path / "gated").mkdir()
    (tmp_path / "gated" / "test_gated.py").write_text("def test_y():\n    pass\n")

    assert _uncovered(tmp_path, covered={"gated"}, quarantined=set()) == [
        "svc/tests"
    ], (
        "the detector did not report the one uncovered directory in a two-directory "
        "tree, so it cannot report a genuinely uncovered suite in the real one either"
    )
    assert (
        _uncovered(tmp_path, covered=set(), quarantined={"svc/tests", "gated"}) == []
    ), (
        "the detector reported directories it was told are quarantined, so the "
        "excluded registry would not be honoured"
    )


def test_every_test_dir_is_run_by_a_ci_recipe():
    module = _run_all_tests_module()
    missing = _uncovered(
        REPO_ROOT,
        covered=_covered_dirs(),
        quarantined=set(module.QUARANTINE),
    )

    assert not missing, (
        "These directories hold test_*.py but are run by neither "
        + " nor ".join(f"`make {target}`" for _, target in CI_RECIPES)
        + " — the only recipes either CI uses to run this repository's offline "
        "suites — so their tests run in NEITHER CI:\n  "
        + "\n  ".join(missing)
        + "\n\nAdd each to the test-packages-cicd recipe in the root Makefile. Give "
        "each its own `cd <dir> && $(PYTEST_HERMETIC) ...` line if it defines a "
        "module named `index` or ships its own conftest, because a combined "
        "invocation fails collection on the basename collision. Every invocation "
        "must go through $(PYTEST_HERMETIC): scripts/tests/"
        "test_offline_suites_are_hermetic.py asserts that, and it will also collect "
        "your suite with the AWS environment stripped, which is where an "
        "import-time boto3 client shows up. If a suite genuinely cannot run in the "
        "shared gate, add it to QUARANTINE in scripts/run_all_tests.py with a "
        "reason instead."
    )


def test_quarantined_dirs_carry_a_reason():
    """An exemption without a stated reason is indistinguishable from an oversight."""
    module = _run_all_tests_module()
    for path, reason in module.QUARANTINE.items():
        assert reason and reason.strip(), f"QUARANTINE['{path}'] has no reason"
