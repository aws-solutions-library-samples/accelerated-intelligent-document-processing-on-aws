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

There are two acceptable ways for a gate to satisfy this. The better one is to reuse
``scripts/run_all_tests.PRUNE_DIR_MARKERS``, which has carried both names all along
and is what ``test_testing_doc.py`` uses — one list to maintain. The other is a local
prune set naming them directly, which is what the six template/source gates do. Those
are not converted to the shared list here because they match on *directory names*
(``set(path.parts)``) while ``PRUNE_DIR_MARKERS`` matches on *path substrings*
(``"/scratch/"``), so the conversion is a behaviour change in six working gates and
belongs in its own commit rather than riding along with a release validation.

The per-gate half is therefore a *textual* check on each gate's source rather than an
import-and-inspect: the constants are spelled differently on purpose (`_SKIP_DIRS`,
`PRUNED_DIRS`, `PRUNE`, a local `skip_dirs`, an inline set literal), and normalising
them would mean changing five working gates to suit their test. What matters is that
the two names appear in the discovery code, which is what a reader checking "does this
gate scan my worktree?" would look for.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent

#: The shared list a gate may use instead of its own prune set.
SHARED_PRUNE_CONST = "PRUNE_DIR_MARKERS"


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
