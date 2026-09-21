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
CI-invoked target or it is quarantined with a reason. Those reasons are matched to
**exactly** the directory they name and never to the tree below it — see
:func:`_uncovered`, where getting that wrong would have exempted this repository's
whole gate layer on the strength of one sentence about one file.

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
import re
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


def _recipe_body(makefile: Path, target: str, root: Path | None = None) -> list[str]:
    """The tab-indented lines of one recipe, with ``\\`` continuations joined."""
    lines = makefile.read_text(encoding="utf-8").splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.startswith(target + ":")]
    assert len(starts) == 1, (
        f"expected exactly one '{target}:' rule in "
        f"{makefile.relative_to(root or REPO_ROOT)}, found {len(starts)}"
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


def _covered_dirs(
    root: Path | None = None,
    recipes: tuple[tuple[str, str], ...] = CI_RECIPES,
) -> set[str]:
    """Repo-relative directories the CI recipes actually run pytest against.

    A pytest target that names a file is recorded as its parent directory, which is
    what makes the directory-level assertion below correct rather than strict.

    ``root`` and ``recipes`` are arguments so that
    ``test_the_covered_side_credits_only_what_a_recipe_names`` can drive this real
    function against a synthetic Makefile, rather than asserting that the parse works
    by reading the code that implements it. Over-crediting is the direction that turns
    the whole gate into a no-op while every assertion still passes, so it needs a probe
    of its own.
    """
    root = root or REPO_ROOT
    covered: set[str] = set()
    for makefile_rel, target in recipes:
        makefile = root / makefile_rel
        workdir_root = makefile.parent.relative_to(root).as_posix()
        if workdir_root == ".":
            workdir_root = ""
        for line in _runnable(_recipe_body(makefile, target, root)):
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
                if (root / resolved).is_file():
                    resolved = Path(resolved).parent.as_posix()
                covered.add(resolved)
    return covered


def _recipe_workdirs(root: Path | None = None) -> set[str]:
    """Directories the root Makefile ``cd``s into before running pytest, by regex.

    A second, deliberately cruder derivation of the same fact, used only as a floor
    for :func:`_covered_dirs`. It reads the raw file and looks at the ``cd`` target and
    nothing else, so a defect in ``_recipe_body``'s continuation joining, in
    ``_target_paths``' option handling, or in the file-versus-directory resolution
    cannot move it. A floor computed by the machinery it is supposed to bound is not a
    floor, which is what a hardcoded ``> 20`` amounted to when the parse yields 65.
    """
    root = root or REPO_ROOT
    text = (root / "Makefile").read_text(encoding="utf-8")
    workdirs: set[str] = set()
    for line in text.splitlines():
        if not line.startswith("\t") or f"$({PYTEST_VAR})" not in line:
            continue
        stripped = line.lstrip("\t")
        if stripped.startswith("@echo") or stripped.lstrip("@").startswith("#"):
            continue
        match = re.match(r"cd (\S+) &&", stripped)
        if match:
            workdirs.add(match.group(1).rstrip("/"))
    return workdirs


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
    """The detector itself, parameterised on its inputs so a probe can drive it.

    **The two sides match differently, and that asymmetry is the design.**

    *Covered* is a prefix match, because a recipe line that runs a whole tree really
    does run everything under it: ``cd lib/idp_common_pkg && pytest tests/`` is one
    line and it credits 28 nested directories.

    *Quarantined* is **exact membership**, because an exclusion covers only the
    directory somebody decided to exclude. This is the same rule
    ``run_all_tests.classify`` applies for the same reason, and the cost of getting it
    wrong here is larger. ``QUARANTINE`` holds a bare ``"scripts"`` entry whose written
    reason is about one file — the live RBAC harness, whose ``test_email()`` helper
    pytest mis-collects. Under prefix matching that one sentence would exempt
    ``scripts/tests`` (2,382 tests, including this file), ``scripts/sdlc/tests``,
    ``scripts/srt/tests`` and ``scripts/security/tests``, all four of which are in
    ``RUN_ROOTS``: deleting their four recipe lines would leave this suite green while
    the repository's entire gate layer ran on no pull request. That is one
    justification attached to a set where the justification is a property of a single
    member, which is the defect class ``CLAUDE.md`` records as recurring here.
    ``test_a_quarantined_parent_does_not_exempt_its_children`` pins it.
    """
    return sorted(
        directory
        for directory in _test_dirs(root)
        if directory not in quarantined and not _covered_by(directory, covered)
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
    floor = _recipe_workdirs()
    assert floor, (
        "the regex floor matched no `cd <dir> && ...$(PYTEST_HERMETIC)` line in the "
        "Makefile, so it cannot bound anything. Either the recipe stopped using that "
        "shape or this regex is wrong."
    )
    assert len(covered) >= len(floor), (
        f"the recipe parse credited {len(covered)} directories but the raw Makefile "
        f"`cd`s into {len(floor)} distinct ones before running pytest, so the parse is "
        "dropping lines and the coverage assertion below would pass on a subset. "
        f"Missing from the parse: {sorted(floor - covered)}"
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


def test_a_quarantined_parent_does_not_exempt_its_children(tmp_path: Path):
    """An exclusion covers the directory somebody excluded, not the tree below it.

    ``QUARANTINE``'s bare ``"scripts"`` entry is excluded for one file — the live RBAC
    harness pytest mis-collects. Matched as a prefix it would also exempt
    ``scripts/tests``, ``scripts/sdlc/tests``, ``scripts/srt/tests`` and
    ``scripts/security/tests``, which is this repository's whole gate layer and 2,382
    tests including this file; their four recipe lines could then be deleted with this
    suite still green. Nothing in the coverage assertion would notice, because every
    root involved is genuinely in the recipe today — which is why the rule needs its own
    probe rather than being left to the live tree to demonstrate.
    """
    (tmp_path / "scripts" / "tests").mkdir(parents=True)
    (tmp_path / "scripts" / "tests" / "test_gate.py").write_text("def test_g(): pass\n")
    (tmp_path / "scripts" / "test_harness.py").write_text("def test_h(): pass\n")

    assert _uncovered(tmp_path, covered=set(), quarantined={"scripts"}) == [
        "scripts/tests"
    ], (
        "quarantining `scripts` also exempted `scripts/tests`. An exclusion must match "
        "the directory exactly — the same rule run_all_tests.classify applies — or one "
        "sentence about one file silently covers every suite added beneath it."
    )


def test_the_covered_side_credits_only_what_a_recipe_names(tmp_path: Path):
    """Can the covered derivation over-credit? Drive it against a synthetic Makefile.

    This is the direction the other probes miss. Gutting ``_uncovered`` is caught, and
    so is a ``_test_dirs`` that returns nothing — but a ``_covered_dirs`` that credits
    more than the recipe names turns the whole gate into a no-op with every other
    assertion still passing, and it is the shape an over-reading refactor of
    ``_target_paths`` or ``_recipe_body`` would take.

    The synthetic recipe also carries the lines that must credit **nothing**: an
    ``@echo`` that happens to mention the wrapper variable, an ``@#`` comment that names
    a real directory, and an invocation whose only argument is an unexpanded Make
    variable (a pass-through for CI's ``-n auto``, not a path).
    """
    for name in ("named", "unnamed", "echoed", "commented"):
        (tmp_path / name).mkdir()
    (tmp_path / "Makefile").write_text(
        "test-packages-cicd:\n"
        '\t@echo "Running $(PYTEST_HERMETIC) against echoed ..."\n'
        "\t@# commented is deliberately not run: $(PYTEST_HERMETIC) commented\n"
        "\tcd named && $(PYTEST_HERMETIC) -q -p no:cacheprovider\n"
        "\t$(PYTEST_HERMETIC) $(PYTEST_ARGS) -q\n"
    )

    covered = _covered_dirs(tmp_path, recipes=(("Makefile", "test-packages-cicd"),))

    assert "named" in covered, (
        f"a `cd named && $(PYTEST_HERMETIC)` line credited nothing; got {covered}"
    )
    over_credited = covered & {"unnamed", "echoed", "commented"}
    assert not over_credited, (
        f"the parse credited {sorted(over_credited)}, which the recipe does not run "
        "pytest against. A directory named only in an @echo message or an @# comment, "
        "or one never mentioned at all, must not count as covered — otherwise this "
        "gate reports nothing while appearing to check everything."
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
