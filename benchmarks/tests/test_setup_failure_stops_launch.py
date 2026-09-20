# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A setup step that FAILS must stop the expensive step behind it.

`run_matrix.py` used to print ``config <version>: FAIL`` and launch the grid
anyway. The result is worse than a crash: the cell runs against whatever
configuration the stack already holds — and ``Config#bench-*`` names are
deterministic and reused across grids, so a previous grid's version of the same
name is often still there — producing a complete set of plausible numbers
attributed to a configuration that never landed. Nothing in ``runmap.json`` said
so, and those numbers get written down.

The same shape existed in ``register_testset``, which discarded the ``aws s3 cp``
result entirely (not even bound to a name) and then wrote a metadata row
asserting ``status: READY`` and ``fileCount: 1``.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

pytest.importorskip("boto3")

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(BENCH, "harness")
sys.path.insert(0, HARNESS)

import run_matrix  # noqa: E402


class _Proc:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _cell(cell, version):
    # `resolved` is the axis values the index claims; main() copies it into each
    # runmap record, so a cell fixture without it is not a valid cell.
    return {
        "cell": cell,
        "version": version,
        "path": f"/tmp/{version}.yaml",
        "resolved": {"model": "test-model"},
    }


# --------------------------------------------------------------------------
# upload_all_configs: the failure must be RETURNED, not just printed
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_upload_all_configs_returns_the_failed_versions(monkeypatch):
    calls = []

    def fake_upload(stack, version, path, res=None, native=False):
        calls.append(version)
        return version != "bench-b"

    monkeypatch.setattr(run_matrix, "upload_config", fake_upload)
    failed = run_matrix.upload_all_configs(
        "stk", [_cell("c1", "bench-a"), _cell("c2", "bench-b"), _cell("c3", "bench-c")]
    )
    assert failed == ["bench-b"]
    # Deduped by version, and a failure does not abort the remaining uploads:
    # a sibling arm whose config lands is still worth running.
    assert calls == ["bench-a", "bench-b", "bench-c"]


@pytest.mark.unit
def test_upload_all_configs_dedupes_versions(monkeypatch):
    calls = []
    monkeypatch.setattr(
        run_matrix,
        "upload_config",
        lambda s, v, p, res=None, native=False: calls.append(v) or True,
    )
    run_matrix.upload_all_configs(
        "stk", [_cell("c1", "bench-a"), _cell("c2", "bench-a")]
    )
    assert calls == ["bench-a"]


@pytest.mark.unit
def test_upload_all_configs_reports_every_failure_not_just_the_first(monkeypatch):
    monkeypatch.setattr(
        run_matrix, "upload_config", lambda s, v, p, res=None, native=False: False
    )
    failed = run_matrix.upload_all_configs(
        "stk", [_cell("c1", "bench-a"), _cell("c2", "bench-b")]
    )
    assert sorted(failed) == ["bench-a", "bench-b"]


@pytest.mark.unit
def test_failed_versions_select_exactly_the_cells_to_skip(monkeypatch):
    """The skip is per version, so a sibling cell on a good version survives.

    This is the filter main() applies to `pairs`/`ref_pairs`; asserting it here
    pins the semantics (skip the arm, not the matrix) independently of main's
    stack plumbing.
    """
    monkeypatch.setattr(
        run_matrix,
        "upload_config",
        lambda s, v, p, res=None, native=False: v != "bench-b",
    )
    cells = [_cell("c1", "bench-a"), _cell("c2", "bench-b"), _cell("c3", "bench-c")]
    failed = set(run_matrix.upload_all_configs("stk", cells))

    pairs = [(c, "doc1", 0) for c in cells]
    kept = [p for p in pairs if p[0]["version"] not in failed]
    assert [p[0]["cell"] for p in kept] == ["c1", "c3"]
    assert sorted({c["cell"] for c in cells if c["version"] in failed}) == ["c2"]


# --------------------------------------------------------------------------
# register_testset: no READY metadata row for a document that did not upload
# --------------------------------------------------------------------------


class _SpyDDB:
    def __init__(self):
        self.puts = []

    def put_item(self, **kw):
        self.puts.append(kw)


@pytest.mark.unit
def test_register_testset_writes_no_metadata_row_when_the_copy_fails(monkeypatch):
    spy = _SpyDDB()
    monkeypatch.setattr(run_matrix.lib, "ddb", lambda: spy)
    monkeypatch.setattr(
        run_matrix,
        "sh",
        lambda cmd: _Proc(returncode=1, stderr="fatal error: Access Denied"),
    )
    ok = run_matrix.register_testset(
        "stk",
        {"testset_bucket": "b", "tracking_table": "t"},
        "tiny_form",
        "/tmp/tiny_form.pdf",
    )
    assert ok is False
    assert spy.puts == [], (
        "a metadata row claiming status=READY / fileCount=1 was written for a "
        "document whose S3 copy failed"
    )


@pytest.mark.unit
def test_register_testset_writes_the_row_on_success(monkeypatch):
    spy = _SpyDDB()
    monkeypatch.setattr(run_matrix.lib, "ddb", lambda: spy)
    monkeypatch.setattr(run_matrix, "sh", lambda cmd: _Proc(returncode=0))
    ok = run_matrix.register_testset(
        "stk",
        {"testset_bucket": "b", "tracking_table": "t"},
        "tiny_form",
        "/tmp/tiny_form.pdf",
    )
    assert ok is True
    assert len(spy.puts) == 1
    assert spy.puts[0]["Item"]["status"]["S"] == "READY"


# --------------------------------------------------------------------------
# The failure is in the artifact, not only on stderr
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_runmap_records_the_skipped_cells():
    """main() writes these three keys; aggregate._meta must carry them into the
    committed summary, because the runmap itself is gitignored."""
    import inspect

    src = inspect.getsource(run_matrix.main)
    for key in (
        "config_upload_failed_versions",
        "cells_skipped_config_upload",
        "docs_missing_truth",
    ):
        assert f'"{key}"' in src, f"{key} is not written to runmap.json"

    import aggregate

    meta_src = inspect.getsource(aggregate._meta)
    for key in ("config_upload_failed_versions", "cells_skipped_config_upload"):
        assert f'rm.get("{key}")' in meta_src, (
            f"{key} is in runmap.json but never reaches summary.json's meta, "
            "which is the only durable record"
        )


# --------------------------------------------------------------------------
# main()'s enforcement, driven for real
# --------------------------------------------------------------------------
#
# The three behaviours that actually protect a grid live in main(): the
# pairs/ref_pairs filter, the post-drain non-zero exit, and writing the manifest
# BEFORE launching. Asserting them via `inspect.getsource` substrings — or, worse,
# by re-implementing the filter in the test body — left all three unprotected:
# deleting the real filter kept the suite green. Two of them are now extracted as
# plain functions and tested directly; the third is covered by driving main().


@pytest.mark.unit
def test_plan_after_upload_failures_drops_only_the_failed_versions():
    cells = [_cell("c1", "bench-a"), _cell("c2", "bench-b"), _cell("c3", "bench-c")]
    pairs = [(c, "doc1", 0) for c in cells]
    ref_pairs = [(cells[1], "refcorpus", 0)]

    kept, kept_ref, skipped = run_matrix.plan_after_upload_failures(
        pairs, ref_pairs, cells, {"bench-b"}
    )
    assert [p[0]["cell"] for p in kept] == ["c1", "c3"]
    assert kept_ref == [], "a reference pair on a failed version must be dropped too"
    assert skipped == ["c2"]


@pytest.mark.unit
def test_plan_after_upload_failures_is_a_no_op_when_everything_uploaded():
    cells = [_cell("c1", "bench-a")]
    pairs = [(cells[0], "doc1", 0)]
    kept, kept_ref, skipped = run_matrix.plan_after_upload_failures(
        pairs, [], cells, set()
    )
    assert kept == pairs and kept_ref == [] and skipped == []


@pytest.mark.unit
def test_nothing_launchable_aborts_but_only_after_the_manifest_is_written():
    """The filter itself must NOT exit: main() writes the manifest between the two,
    and the total-failure case is the one where the artifact matters most."""
    cells = [_cell("c1", "bench-a")]
    pairs = [(cells[0], "doc1", 0)]
    kept, kept_ref, skipped = run_matrix.plan_after_upload_failures(
        pairs, [], cells, {"bench-a"}
    )
    assert kept == [] and kept_ref == [] and skipped == ["c1"]
    with pytest.raises(SystemExit) as exc:
        run_matrix.assert_something_launchable(kept, kept_ref, {"bench-a"})
    assert "no launchable runs" in str(exc.value)


@pytest.mark.unit
def test_assert_something_launchable_is_silent_when_work_remains():
    run_matrix.assert_something_launchable([("c", "d", 0)], [], {"bench-b"})


@pytest.mark.unit
def test_exit_status_for_grid_fails_when_a_version_did_not_upload():
    runmap = [{"run_id": "r1"}]
    with pytest.raises(SystemExit) as exc:
        run_matrix.exit_status_for_grid({"bench-b"}, ["c2"], runmap)
    assert "incomplete grid" in str(exc.value)


@pytest.mark.unit
def test_exit_status_for_grid_is_silent_on_a_complete_grid():
    run_matrix.exit_status_for_grid(set(), [], [{"run_id": "r1"}])


@pytest.mark.unit
def test_exit_status_for_grid_fails_when_every_launch_was_rejected():
    """A grid whose every launch returned None measured nothing but used to print
    `done.` and exit 0 — the sibling launcher already exits on this."""
    with pytest.raises(SystemExit) as exc:
        run_matrix.exit_status_for_grid(set(), [], [{"run_id": None}, {"run_id": None}])
    assert "no run launched" in str(exc.value)


@pytest.mark.unit
def test_exit_status_for_grid_accepts_a_partial_launch_failure():
    """One rejected launch is already recorded as NOT_LAUNCHED per run; only a
    total failure means the grid measured nothing."""
    run_matrix.exit_status_for_grid(set(), [], [{"run_id": None}, {"run_id": "r2"}])


@pytest.mark.unit
def test_main_writes_the_manifest_and_launches_nothing_when_every_upload_fails(
    tmp_path, monkeypatch
):
    """End-to-end over main(): no launch, a manifest on disk recording the skip,
    and a non-zero exit.

    Driving main() is what covers the pre-launch `_write_runmap()` call, which no
    extracted function can: before it existed, runmap.json first appeared after
    the first successful launch, so a grid that launched nothing left no artifact
    at all.
    """
    cells = [_cell("c1", "bench-a"), _cell("c2", "bench-b")]
    launches = []

    monkeypatch.setattr(run_matrix, "RESULTS", str(tmp_path))
    monkeypatch.setattr(
        run_matrix,
        "resolve_stack",
        lambda stack: {
            "testset_bucket": "b",
            "output_bucket": "o",
            "tracking_table": "t",
            "config_table": "c",
        },
    )
    monkeypatch.setattr(
        run_matrix,
        "load_plan",
        lambda *a, **k: (cells, ["tiny_form"], 1, ["tiny_form"]),
    )
    monkeypatch.setattr(
        run_matrix, "plan_coverage", lambda *a, **k: (["tiny_form"], [], [])
    )
    monkeypatch.setattr(run_matrix, "reference_plan", lambda *a, **k: ({}, []))
    monkeypatch.setattr(run_matrix, "register_testset", lambda *a, **k: True)
    monkeypatch.setattr(run_matrix, "verify_config_axes", lambda cells: None)
    monkeypatch.setattr(
        run_matrix, "assert_stack_quiesced", lambda stack: ("OK", "", "")
    )
    monkeypatch.setattr(run_matrix, "assert_stack_unchanged", lambda *a, **k: None)
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    # Every upload fails.
    monkeypatch.setattr(run_matrix, "upload_config", lambda *a, **k: False)
    monkeypatch.setattr(
        run_matrix, "launch", lambda *a, **k: launches.append(a) or "SHOULD-NOT-HAPPEN"
    )
    monkeypatch.setattr(sys, "argv", ["run_matrix.py", "--stack", "stk"])

    with pytest.raises(SystemExit) as exc:
        run_matrix.main()

    assert launches == [], (
        "main() launched runs after every configuration upload failed — the exact "
        "defect: the grid would measure whatever config the stack already held"
    )
    assert "no launchable runs" in str(exc.value)

    runmaps = list(tmp_path.glob("*/runmap.json"))
    assert len(runmaps) == 1, (
        f"expected exactly one runmap.json written before launching, found {runmaps}"
    )
    rm = json.loads(runmaps[0].read_text())
    assert sorted(rm["config_upload_failed_versions"]) == ["bench-a", "bench-b"]
    assert rm["cells_skipped_config_upload"] == ["c1", "c2"]
    assert rm["runs"] == []


@pytest.mark.unit
def test_main_runs_the_surviving_arm_and_still_exits_nonzero(tmp_path, monkeypatch):
    """The partial case: one version fails, its sibling still runs, the manifest
    names the skipped cell, and the exit status is still non-zero."""
    cells = [_cell("c1", "bench-a"), _cell("c2", "bench-b")]
    launched = []

    monkeypatch.setattr(run_matrix, "RESULTS", str(tmp_path))
    monkeypatch.setattr(
        run_matrix,
        "resolve_stack",
        lambda stack: {
            "testset_bucket": "b",
            "output_bucket": "o",
            "tracking_table": "t",
            "config_table": "c",
        },
    )
    monkeypatch.setattr(
        run_matrix,
        "load_plan",
        lambda *a, **k: (cells, ["tiny_form"], 1, ["tiny_form"]),
    )
    monkeypatch.setattr(
        run_matrix, "plan_coverage", lambda *a, **k: (["tiny_form"], [], [])
    )
    monkeypatch.setattr(run_matrix, "reference_plan", lambda *a, **k: ({}, []))
    monkeypatch.setattr(run_matrix, "register_testset", lambda *a, **k: True)
    monkeypatch.setattr(run_matrix, "verify_config_axes", lambda cells: None)
    monkeypatch.setattr(
        run_matrix, "assert_stack_quiesced", lambda stack: ("OK", "", "")
    )
    monkeypatch.setattr(run_matrix, "assert_stack_unchanged", lambda *a, **k: None)
    monkeypatch.setattr(os.path, "exists", lambda p: True)
    monkeypatch.setattr(
        run_matrix, "upload_config", lambda s, v, p, **k: v != "bench-b"
    )

    def _launch(stack, testset, version, ctx, number_of_files=1):
        launched.append(version)
        return f"run-{version}"

    monkeypatch.setattr(run_matrix, "launch", _launch)
    # poll_runs is keyed by run id; returning {} makes prune_pending KeyError.
    monkeypatch.setattr(
        run_matrix.lib, "poll_runs", lambda table, ids, *a, **k: {i: {} for i in ids}
    )
    monkeypatch.setattr(run_matrix, "run_complete", lambda *a, **k: True)
    monkeypatch.setattr(sys, "argv", ["run_matrix.py", "--stack", "stk"])

    with pytest.raises(SystemExit) as exc:
        run_matrix.main()

    assert launched == ["bench-a"], (
        f"expected only the arm whose config uploaded to run, got {launched}"
    )
    assert "incomplete grid" in str(exc.value)

    rm = json.loads(next(tmp_path.glob("*/runmap.json")).read_text())
    assert rm["config_upload_failed_versions"] == ["bench-b"]
    assert rm["cells_skipped_config_upload"] == ["c2"]
    assert [r["cell"] for r in rm["runs"]] == ["c1"]


# --------------------------------------------------------------------------
# upload_config reads the exit code, not the console text
# --------------------------------------------------------------------------


@pytest.mark.unit
def test_upload_config_trusts_the_exit_code_over_a_wrapped_message(monkeypatch):
    """`rich.Console` wraps at COLUMNS, so the success line is not a reliable token.

    `✓ Configuration uploaded successfully` is 37 characters; in a narrow or
    non-tty terminal Rich breaks it mid-string, so a substring test reports FAIL
    for an upload that succeeded. That used to cost a misleading console line —
    now a FAIL skips the cell and fails the grid, so the false negative is
    expensive.
    """
    wrapped = "✓ Configuration uploaded\nsuccessfully\n"
    assert "uploaded successfully" not in wrapped, "fixture no longer wraps"
    monkeypatch.setattr(
        run_matrix, "sh", lambda cmd: _Proc(returncode=0, stdout=wrapped)
    )
    assert run_matrix.upload_config("stk", "bench-a", "/tmp/a.yaml") is True, (
        "a successful upload whose console output wrapped was reported as FAIL"
    )


@pytest.mark.unit
def test_upload_config_fails_on_nonzero_exit_despite_a_success_message(monkeypatch):
    """The converse: the message can appear in output that still exited non-zero
    (a retry log, a later error), and the exit code is what `cli.py` sets."""
    monkeypatch.setattr(
        run_matrix,
        "sh",
        lambda cmd: _Proc(
            returncode=1,
            stdout="Configuration uploaded successfully\n",
            stderr="✗ Error: ResourceNotFoundException\n",
        ),
    )
    assert run_matrix.upload_config("stk", "bench-a", "/tmp/a.yaml") is False


@pytest.mark.unit
def test_upload_config_succeeds_on_zero_exit(monkeypatch):
    monkeypatch.setattr(
        run_matrix,
        "sh",
        lambda cmd: _Proc(returncode=0, stdout="✓ Configuration uploaded successfully"),
    )
    assert run_matrix.upload_config("stk", "bench-a", "/tmp/a.yaml") is True
