# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every guard that walks the whole repo must skip gitignored local work.

Several gates in this directory assert a property over *every* file in the tree —
every template declares a log group, every `${...}` substitution survives, no
template grants IAM mutation, every `normalize_event` consumer catches the refusal.
Each one discovers its inputs with `REPO_ROOT.rglob(...)`, and each one therefore
also walks two directories that are gitignored and routinely contain whole copies
of this repository:

* ``scratch/`` — local verification work, including `git worktree` checkouts and
  mutation-test mutants (`scratch/p806_verify/mutants/*/idp_common/api_adapter.py`);
* ``.claude/worktrees/`` — the assistant's per-task worktrees.

A copy under either path ships nothing, but it parses exactly like the real thing,
so a stale one fails the gate. This is not hypothetical and it is not rare: the
v0.6.8 release validation lost two rules to worktrees under ``scratch/``, and the
0.6.9 validation opened with **158** failures of which **157** came from 74 agent
worktrees under ``.claude/`` — one real defect buried under a hundredfold of noise,
in the one gate set whose whole job is to be believed.

The per-gate fix was applied five times and drifted five ways: two gates had no
`scratch` entry, four had no `.claude` entry, one had no prune set at all, and one
(`test_classification_prompt_copies_in_sync.py`) had the complete set all along.
That is the shape this file exists to stop — the fix belongs to the class, so the
assertion does too.

There are three acceptable ways for a gate to satisfy this. The best is to discover
through ``repo_files.tracked_paths``, which asks git and so cannot be defeated by
where the checkout happens to sit — see below, and see ``repo_files.py``. The second
is to reuse ``scripts/run_all_tests.PRUNE_DIR_MARKERS``, which has carried both names
all along and is what ``test_testing_doc.py`` uses — one list to maintain. The third
is a local prune set naming them directly, which is what the remaining template and
source gates do. Those are not converted to the shared list here because they match on
*directory names* (``set(path.parts)``) while ``PRUNE_DIR_MARKERS`` matches on *path
substrings* (``"/scratch/"``), so the conversion is a behaviour change in working
gates and belongs in its own commit rather than riding along with a release
validation.

The per-gate half is therefore a *textual* check on each gate's source rather than an
import-and-inspect: the constants are spelled differently on purpose (`_SKIP_DIRS`,
`PRUNED_DIRS`, `PRUNE`, a local `skip_dirs`, an inline set literal), and normalising
them would mean changing working gates to suit their test. What matters is that the
two names appear in the discovery code, which is what a reader checking "does this
gate scan my worktree?" would look for.

Pruning fails in one direction the textual check cannot see
---------------------------------------------------------
A prune set is only correct when it is matched against a file's **repo-relative**
path. Matched against the absolute path, the same set matches a directory name
*above* the repository root — and an agent worktree is a real checkout at
``<main checkout>/.claude/worktrees/agent-*``, so from inside one, ``.claude``
appears in every absolute path and the gate discards its entire input. Three gates
were in that state. Two of them have a "discovery found nothing, so this proves
nothing" self-guard and failed loudly, on every task in a batch; the third has none
and passed **vacuously**, which is the worse of the two outcomes and the reason a
textual check is not enough on its own.

``test_discovery_survives_a_checkout_under_a_local_work_dir`` is the half that
catches it: it builds a real one-file checkout at
``<tmp>/.claude/worktrees/agent-probe`` and asserts each registered gate's discovery
still finds the file. A gate whose prune set is applied to the absolute path returns
nothing there and fails this test in CI, where no ``.claude`` path is involved at all.
"""

from __future__ import annotations

import importlib
import importlib.util
import subprocess
from pathlib import Path

import gate_premises
import pytest
import repo_files

REPO_ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent

#: The shared list a gate may use instead of its own prune set.
SHARED_PRUNE_CONST = "PRUNE_DIR_MARKERS"

#: The git-backed discovery helper a gate may use instead of walking at all.
TRACKED_HELPER = "tracked_paths"


def _run_all_tests_module():
    """Load ``scripts/run_all_tests.py`` by path (it is not an importable package)."""
    path = REPO_ROOT / "scripts" / "run_all_tests.py"
    spec = importlib.util.spec_from_file_location("_run_all_tests", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Gitignored directories that can hold a whole copy of this tree.
#:
#: Local work is two of them; the third is written by this repository's own tooling
#: and is derived from :data:`TREE_COPY_DIRS` below rather than restated, so adding
#: an entry there makes every registered gate prune it without a second edit. That
#: derivation is the point: `.ai-review` was first fixed for the git discovery route
#: only, which left five filesystem-walking gates reading the merge-base copy — the
#: instance-not-class shape the registry entry for TREE_COPY_DIRS argues against.
_LOCAL_WORK_DIRS_PROPER = ("scratch", ".claude")

#: Directories into which this repository's **own tooling** writes a whole second
#: copy of the tree, each paired with the file that creates it.
#:
#: These are the case the git route does not cover for free. Discovering through
#: ``repo_files.tracked_paths`` is stronger than any name list — but only because
#: every such copy is gitignored, and ``tracked_paths`` lists ``--others``, so
#: *untracked* is not enough. A tree copy in a directory nobody ignored is read as
#: part of the branch by every gate that discovers through it.
#:
#: ``.ai-review/base/`` is the live example and the reason this is pinned: the
#: advisory reviewer checks out the whole merge-base tree there, so inside a review
#: worktree a repo-wide gate sees the state *before* the branch alongside the state
#: after it, and a defect the branch FIXES is reported against it. The creating file
#: is named so a rename there fails here rather than leaving a dead ignore rule.
TREE_COPY_DIRS = ((".ai-review", "scripts/sdlc/ai_mr_review.py"),)

#: Every directory a repo-walking gate must prune, local work plus tree copies.
LOCAL_WORK_DIRS = _LOCAL_WORK_DIRS_PROPER + tuple(d for d, _ in TREE_COPY_DIRS)

#: Gates that walk the repo and must therefore prune local work. Each entry is a
#: filename in this directory. A gate added later that calls `rglob` on the repo
#: root is caught by :func:`test_no_unregistered_repo_walking_gate` below, so this
#: list cannot silently fall behind.
REPO_WALKING_GATES = (
    "test_api_adapter_identity_provenance.py",
    "test_asl_placeholder_substitution.py",
    "test_classification_prompt_copies_in_sync.py",
    "test_iam_privilege_escalation.py",
    "test_lambda_log_groups.py",
    "test_log_group_encryption.py",
    "test_no_outbound_aws_is_universal.py",
    "test_testing_doc.py",
    "test_well_architected_doc.py",
    # Outside this directory: it globs *.yaml from the repo root to check that every
    # alarm names a real metric. Found by the gate-exemption registry's discovery, not
    # by this file's own sweep, which only looks at scripts/tests/ -- so the sweep below
    # is narrower than the class and that is recorded in its docstring.
    "../../lib/idp_sdk/tests/unit/test_cloudwatch_alarms.py",
)


@pytest.mark.unit
@pytest.mark.parametrize("gate", REPO_WALKING_GATES)
@pytest.mark.parametrize("local_dir", LOCAL_WORK_DIRS)
def test_gate_prunes_local_work(gate: str, local_dir: str) -> None:
    path = HERE / gate
    assert path.exists(), f"{gate} is registered here but does not exist"
    source = path.read_text(encoding="utf-8")
    if SHARED_PRUNE_CONST in source:
        # Delegates to the shared list, which is pinned by the test below.
        return
    if TRACKED_HELPER in source:
        # Discovers through git, which excludes both directories because both are
        # gitignored — a stronger guarantee than any name list, and the one route
        # that also survives the checkout itself living under one of them. Named
        # here so a gate that takes the better route is not then required to keep
        # a prune set it no longer needs; the three that do keep one keep it as a
        # second filter for their non-git fallback, and say so.
        return
    assert f'"{local_dir}"' in source or f"'{local_dir}'" in source, (
        f"{gate} walks the whole repository but never names {local_dir!r}, so it "
        f"scans gitignored local work. A git worktree or a mutation-test copy under "
        f"{local_dir}/ is a full copy of this tree: it will fail the gate while "
        f"describing nothing that ships. Add {local_dir!r} to the gate's prune set, "
        f"or discover through {SHARED_PRUNE_CONST} (see the module docstring)."
    )


@pytest.mark.unit
@pytest.mark.parametrize("local_dir", LOCAL_WORK_DIRS)
def test_shared_prune_list_covers_local_work(local_dir: str) -> None:
    """The list the gates may delegate to must itself carry both names."""
    markers = _run_all_tests_module().PRUNE_DIR_MARKERS
    assert any(f"/{local_dir}" in marker for marker in markers), (
        f"run_all_tests.{SHARED_PRUNE_CONST} does not prune {local_dir!r}, so every "
        f"gate that delegates to it — and `make test`'s own root discovery — walks "
        f"gitignored local work. Markers: {markers}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("local_dir", LOCAL_WORK_DIRS)
def test_the_tracked_helpers_fallback_walk_prunes_local_work(
    local_dir: str, tmp_path: Path
) -> None:
    """The other shared mechanism must carry both names too, and apply them.

    ``repo_files.tracked_paths`` is the route this file recommends, and a gate that
    takes it is excused above from naming the two directories itself. That excuse is
    only sound if the helper covers them on BOTH of its paths. Asking git covers them
    because both are gitignored; the fallback walk — taken when the root is not the
    top of a checkout, which is how every gate's own synthetic-tree test drives its
    discovery — has nothing but ``repo_files.FALLBACK_PRUNE_DIRS``. That set stands in
    for five per-gate prune sets and was the only one with no assertion behind it.

    Driven against a real tree rather than read out of the module, because membership
    is not the property that failed five times: the set has to be matched against the
    REPO-RELATIVE path, and matching it against the absolute path is what discarded
    whole checkouts. Both halves are asserted here for the same reason they are below —
    "prunes everything" would satisfy the exclusion half on its own.
    """
    root = tmp_path.resolve()
    assert local_dir in repo_files.FALLBACK_PRUNE_DIRS, (
        f"repo_files.FALLBACK_PRUNE_DIRS does not name {local_dir!r}, so every gate "
        f"that discovers through repo_files.{TRACKED_HELPER} walks gitignored local "
        f"work whenever its root is not a checkout. Set: "
        f"{sorted(repo_files.FALLBACK_PRUNE_DIRS)}"
    )

    (root / "svc").mkdir()
    (root / "svc" / "handler.py").write_text("x = 1\n", encoding="utf-8")
    buried = root / local_dir / "worktrees" / "agent-probe" / "svc"
    buried.mkdir(parents=True)
    (buried / "handler.py").write_text("x = 1\n", encoding="utf-8")

    assert repo_files._git_toplevel(root) != root, (
        "this probe tree is itself a checkout, so tracked_paths would ask git and the "
        "fallback walk — the thing under test — would not run at all"
    )
    found = {
        p.relative_to(root).as_posix() for p in repo_files.tracked_paths(root, "*.py")
    }

    assert "svc/handler.py" in found, (
        f"the fallback walk found {sorted(found) or 'nothing'} and not the probe at "
        f"the root of the tree, so its prune set is being matched against something "
        f"other than the repo-relative path"
    )
    leaked = sorted(rel for rel in found if rel.startswith(f"{local_dir}/"))
    assert not leaked, (
        f"the fallback walk returned {leaked} from {local_dir}/, which holds whole "
        f"copies of this tree: a stale one fails the gate while describing nothing "
        f"that ships"
    )


#: Gates whose discovery takes a root and can therefore be driven against a
#: synthetic checkout. Each entry is
#: ``gate filename -> (discovery function, probe file, expected result)``.
#:
#: Hand-kept, like ``REPO_WALKING_GATES`` above, because there is no way to call an
#: arbitrary gate's discovery without knowing its entry point. The three here are the
#: three that had the absolute-path defect. Of the five other registered gates, four
#: cannot be defeated by where the checkout sits — two match their prune set against
#: the repo-relative path (``test_asl_placeholder_substitution.py``,
#: ``test_well_architected_doc.py``), one prunes during an ``os.walk`` descent from
#: the repository root (``test_iam_privilege_escalation.py``), and one asks git
#: (``test_classification_prompt_copies_in_sync.py``) — and none of the four exposes
#: a root parameter to drive. The fifth, ``test_testing_doc.py``, relativises before
#: matching as well.
_PROBE_TEMPLATE = """AWSTemplateFormatVersion: '2010-09-09'
Resources:
  ProbeFunction:
    Type: AWS::Serverless::Function
    Properties:
      CodeUri: ./
      Handler: index.handler
  ProbeLogGroup:
    Type: AWS::Logs::LogGroup
    Properties:
      RetentionInDays: 30
"""

TRACKED_DISCOVERY_GATES: dict[str, tuple[str, str, str]] = {
    "test_api_adapter_identity_provenance.py": (
        "_python_sources",
        "svc/handler.py",
        "x = 1\n",
    ),
    "test_log_group_encryption.py": (
        "_discover_log_group_templates",
        "svc/template.yaml",
        _PROBE_TEMPLATE,
    ),
    "test_lambda_log_groups.py": (
        "_discover_unlisted_templates",
        "svc/template.yaml",
        _PROBE_TEMPLATE,
    ),
}


def _gate_module(gate: str):
    """The already-collected gate module, by its pytest import name."""
    return importlib.import_module(Path(gate).stem)


def _relative_results(root: Path, results) -> set[str]:
    """Normalise a discovery result to repo-relative posix strings.

    The three functions differ: one yields absolute ``Path``s, two return
    repo-relative strings. Normalising here rather than requiring one shape keeps
    this test from dictating the gates' internal signatures.
    """
    out = set()
    for item in results:
        path = Path(item)
        out.add(
            path.relative_to(root).as_posix() if path.is_absolute() else path.as_posix()
        )
    return out


@pytest.mark.unit
@pytest.mark.parametrize("gate", sorted(TRACKED_DISCOVERY_GATES))
def test_discovery_survives_a_checkout_under_a_local_work_dir(
    gate: str, tmp_path: Path
) -> None:
    """A checkout that *lives* under ``.claude/`` must still be discovered.

    The mirror image of the pruning rule above, and the direction the textual check
    cannot see. An agent worktree is a real ``git worktree`` checkout at
    ``<main checkout>/.claude/worktrees/agent-*``. A prune set matched against the
    absolute path matches ``.claude`` there and discards every file, so the gate
    proves nothing — loudly if it has a self-guard, silently if it does not.

    Both halves are asserted, because on its own the first half is satisfied by a
    gate that has stopped pruning altogether: the probe at the checkout root must be
    **found**, and identical probes under ``<root>/scratch/`` and
    ``<root>/.claude/worktrees/`` must be **excluded**. That pair is also why the
    three converted gates keep a prune set at all — git lists an un-ignored
    ``scratch/`` inside a checkout perfectly happily, so the relative-path filter is
    still doing work.
    """
    function_name, probe_rel, probe_body = TRACKED_DISCOVERY_GATES[gate]

    root = tmp_path / ".claude" / "worktrees" / "agent-probe"
    local_work = ["scratch", ".claude/worktrees/agent-nested"]
    for prefix in ["", *local_work]:
        target = root / prefix / probe_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(probe_body, encoding="utf-8")
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", "-q", str(root)],
        check=True,
        capture_output=True,
    )
    assert ".claude" in root.parts, "the probe root must sit under a local-work dir"

    discover = getattr(_gate_module(gate), function_name)
    try:
        results = discover(root)
    except TypeError as exc:  # pragma: no cover - only on an unconverted gate
        pytest.fail(
            f"{gate}: {function_name}() does not accept a root to discover from "
            f"({exc}), so this test cannot drive it against a synthetic checkout. "
            f"Give it a `root` parameter defaulting to the repository root — the "
            f"other two gates in TRACKED_DISCOVERY_GATES already have one."
        )
    found = _relative_results(root, results)

    assert probe_rel in found, (
        f"{gate}: {function_name}() found {sorted(found) or 'nothing'} in a checkout "
        f"at {root}, which contains a '.claude' path component above its root. A "
        f"prune set naming '.claude' must be matched against the REPO-RELATIVE path, "
        f"or discovery returns nothing whenever the checkout itself lives under one "
        f"of the local-work directories — which is where every agent worktree lives. "
        f"Prefer discovering through repo_files.{TRACKED_HELPER}, which asks git and "
        f"so cannot be defeated by the checkout's location."
    )

    leaked = sorted(f"{prefix}/{probe_rel}" for prefix in local_work)
    leaked = sorted(rel for rel in leaked if rel in found)
    assert not leaked, (
        f"{gate}: {function_name}() returned file(s) from a local-work directory "
        f"INSIDE the checkout: {leaked}. Those directories hold whole copies of this "
        f"tree, so a stale one fails the gate while describing nothing that ships. "
        f"Discovering through git is not enough on its own here — an un-ignored "
        f"`scratch/` in a checkout is listed by `git ls-files --others` — so keep the "
        f"relative-path prune set as a second filter."
    )


@pytest.mark.unit
@pytest.mark.parametrize("directory, creator", TREE_COPY_DIRS)
def test_a_tree_copy_directory_is_gitignored(directory: str, creator: str) -> None:
    """The premise the git route rests on, computed rather than assumed.

    Through ``gate_premises.vcs_ignored_build_output`` rather than a local
    ``check-ignore`` call, because that predicate measures the half a bare probe
    misses — that git tracks nothing *under* the path, which is what makes the
    exclusion free — and it probes with a trailing slash, so a ``foo/`` rule is
    not reported as absent merely because the directory is not on disk.
    """
    holds, detail = gate_premises.vcs_ignored_build_output(directory)
    assert holds, (
        f"{directory}/: {detail}. {creator} writes a whole copy of this tree "
        f"there, and `repo_files.{TRACKED_HELPER}` lists `--others`, so every "
        f"repo-wide gate that discovers through it then reads that second copy as "
        f"part of the branch — including the pre-change state of files the branch "
        f"fixes. Add `{directory}/` to .gitignore."
    )


@pytest.mark.unit
@pytest.mark.parametrize("directory, creator", TREE_COPY_DIRS)
def test_a_tree_copy_directory_is_also_in_the_fallback_prune_list(
    directory: str, creator: str
) -> None:
    """The non-git branch needs the same name, for the case it exists for.

    Being gitignored settles the git route and nothing else. ``tracked_paths``
    walks when its root is not a checkout top, and that walk is filtered only by
    :data:`repo_files.FALLBACK_PRUNE_DIRS` — so a name present in one route and
    absent from the other is covered exactly while the primary route works, which
    is not what a fallback is for.
    """
    assert directory in repo_files.FALLBACK_PRUNE_DIRS, (
        f"{directory!r} is gitignored but missing from "
        f"repo_files.FALLBACK_PRUNE_DIRS, so the non-git walk still descends into "
        f"the whole copy of the tree {creator} writes there. Both routes or "
        f"neither: {sorted(repo_files.FALLBACK_PRUNE_DIRS)}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("directory, creator", TREE_COPY_DIRS)
def test_the_file_that_writes_the_tree_copy_still_names_it(
    directory: str, creator: str
) -> None:
    """A ratchet on the entry above: an ignore rule for a directory nothing writes
    any more is dead, and a dead one pre-ignores whatever next takes the name."""
    path = REPO_ROOT / creator
    assert path.exists(), f"{creator} is registered here but does not exist"
    assert directory in path.read_text(encoding="utf-8"), (
        f"{creator} no longer mentions {directory!r}, so either it was renamed — in "
        f"which case the .gitignore rule now protects nothing and the new name is "
        f"unprotected — or the copy is no longer written and both this entry and "
        f"the ignore rule should go."
    )


@pytest.mark.unit
@pytest.mark.parametrize("directory, creator", TREE_COPY_DIRS)
def test_tracked_discovery_does_not_list_a_tree_copy(
    directory: str, creator: str
) -> None:
    """The behavioural half: a real file under the directory, in this checkout.

    The two assertions above read configuration. This one drives the helper the
    gates actually call, because that is the thing whose answer matters, and
    it is the assertion that fails on the state this was written for.

    It writes a real ``.py`` into the checkout for the duration, which is the
    concurrent-interference shape CLAUDE.md warns about for ``make srt-scan``
    overlapping the offline suite. Checked rather than assumed: the probe is
    gitignored, so ``ruff`` skips it natively, and ``basedpyright``'s ``include``
    never reaches this directory — so neither whole-tree gate can see it. It is
    removed in a ``finally``, along with the directories it had to create.

    The window in which the file exists is **accepted**, not overlooked: every
    gate that could read a stray ``.py`` honours the ignore file or never walks
    here, and a hard-killed run leaves behind one gitignored file that the next
    run of this test overwrites. The alternative — a synthetic checkout in
    ``tmp_path`` — would assert against an ignore file this test wrote, which is
    not the claim. The claim is about **this** repository's ``.gitignore``.
    """
    probe = REPO_ROOT / directory / "base" / "scripts" / "probe_tree_copy.py"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text("x = 1\n", encoding="utf-8")
    try:
        found = repo_files.tracked_paths(REPO_ROOT, "*.py")
    finally:
        # missing_ok: two concurrent runs of this suite in one checkout would
        # otherwise have the second's `finally` raise FileNotFoundError and error
        # a test that had already passed.
        probe.unlink(missing_ok=True)
        for parent in list(probe.parents):
            if parent == REPO_ROOT:
                break
            try:
                parent.rmdir()
            except OSError:
                break

    assert probe not in found, (
        f"repo_files.{TRACKED_HELPER} listed a file under {directory}/, which "
        f"{creator} fills with a whole copy of this tree. Every repo-wide gate "
        f"discovering through it is then asserting over two trees at once."
    )


@pytest.mark.unit
def test_no_unregistered_repo_walking_gate() -> None:
    """A new gate that walks the repo root must be registered above.

    Without this, the parametrised check above silently covers only the gates that
    happened to exist when it was written — the same "control that is never
    consulted" failure it is meant to prevent.
    """
    unregistered = []
    for path in sorted(HERE.glob("test_*.py")):
        if path.name == Path(__file__).name or path.name in REPO_WALKING_GATES:
            continue
        source = path.read_text(encoding="utf-8")
        # The two spellings used in this directory to walk the tree from its root.
        if "REPO_ROOT.rglob(" in source or "_REPO.rglob(" in source:
            unregistered.append(path.name)

    assert not unregistered, (
        "these gates walk the repository root but are not in REPO_WALKING_GATES, so "
        f"nothing checks that they prune gitignored local work: {unregistered}. Add "
        "them to the list and give them the prune set, or use a discovery helper "
        "that already prunes."
    )
