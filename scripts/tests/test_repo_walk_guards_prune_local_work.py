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

import pytest

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
LOCAL_WORK_DIRS = ("scratch", ".claude")

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
    "test_testing_doc.py",
    "test_well_architected_doc.py",
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
