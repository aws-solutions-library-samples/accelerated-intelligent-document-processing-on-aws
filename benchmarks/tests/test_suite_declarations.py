# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every benchmark suite must be RUNNABLE, and every A/B suite must have two arms.

This file exists because both failures had already happened and neither was
visible until someone tried to run the suite:

* ``forcing`` (the WS-05 A/B) declared cells but no ``docs:``, so
  ``run_matrix.load_plan`` raised ``KeyError: 'docs'`` on the first line that
  touched it. The suite had been committed, reviewed, and described in a comment
  that named the documents it would run on — it just could not run.
* ``boundaryctl`` named the cell ``split-llm-legacyprompt``, which was not in
  ``core_cells``. ``make_configs.cells_for_suite`` filtered unknown ids with
  ``if i in core``, so the suite silently generated ZERO configs. A typo in any
  A/B suite's cell list does the same thing quietly: the run proceeds with one
  arm, and a one-armed A/B produces a number with nothing to compare it to.

The second is the dangerous shape. A benchmark that crashes gets fixed; a
benchmark that silently drops the control arm gets *published*. So this file
asserts the declaration is complete and the harness is loud, and
:func:`test_the_guard_is_not_vacuous` checks the loudness itself.
"""

from __future__ import annotations

import os
import sys

import pytest

yaml = pytest.importorskip("yaml")

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(BENCH, "harness")
sys.path.insert(0, HARNESS)

CFG_MATRIX = os.path.join(BENCH, "matrices", "config_matrix.yaml")
DOC_MATRIX = os.path.join(BENCH, "matrices", "doc_matrix.yaml")


@pytest.fixture(scope="module")
def matrix():
    with open(CFG_MATRIX) as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def docm():
    with open(DOC_MATRIX) as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="module")
def suites(matrix):
    return matrix["suites"]


def _cell_ids(matrix):
    """Every id a suite may name: the core grid plus the control arms."""
    return (
        {c["id"] for c in matrix["core_cells"]}
        | {c["id"] for c in matrix.get("control_cells") or []}
        # Multi-instance cells (#715/#753) are their own registry, for the same
        # reason control_cells is: they are only meaningful on a document holding
        # several records of one class in ONE section, so a suite saying
        # `cells: "core_cells"` must not pick them up.
        | {c["id"] for c in matrix.get("multi_instance_cells") or []}
    )


def _doc_ids(docm):
    return {d["id"] for d in docm.get("synthetic", [])} | {
        d["id"] for d in docm.get("reference", [])
    }


@pytest.mark.unit
def test_every_suite_declares_docs(suites):
    """The `forcing` regression: cells without docs is not a runnable suite."""
    missing = [name for name, spec in suites.items() if "docs" not in spec]
    assert not missing, (
        f"suite(s) {missing} declare no `docs:`. run_matrix.load_plan cannot "
        f"build a plan without them."
    )


@pytest.mark.unit
def test_every_suite_declares_cells(suites):
    missing = [name for name, spec in suites.items() if "cells" not in spec]
    assert not missing, f"suite(s) {missing} declare no `cells:`"


@pytest.mark.unit
def test_every_named_cell_exists(matrix, suites):
    """The `boundaryctl` regression: an unknown cell id used to vanish silently."""
    known = _cell_ids(matrix)
    bad = {
        name: [c for c in spec["cells"] if c not in known]
        for name, spec in suites.items()
        if isinstance(spec.get("cells"), list)
    }
    bad = {k: v for k, v in bad.items() if v}
    assert not bad, (
        f"suite(s) name cells that are not defined in core_cells / "
        f"control_cells / multi_instance_cells: {bad}. "
        f"An undefined cell is dropped, which silently removes an A/B arm."
    )


@pytest.mark.unit
def test_every_named_doc_exists(docm, suites):
    known = _doc_ids(docm)
    groups = set(docm.get("groups", {}))
    bad = {}
    for name, spec in suites.items():
        ds = spec.get("docs")
        if isinstance(ds, list):
            unknown = [d for d in ds if d not in known and d not in groups]
        elif isinstance(ds, str):
            unknown = [] if ds in groups or ds == "all" else [ds]
        else:
            unknown = []
        if unknown:
            bad[name] = unknown
    assert not bad, f"suite(s) name documents absent from doc_matrix.yaml: {bad}"


@pytest.mark.unit
def test_every_cell_uses_only_declared_axes(matrix):
    """A cell keyed on a misspelled axis is ignored by ``make_configs``: the knob
    is never applied and the cell is a duplicate of the default, which reads as
    "the feature made no difference"."""
    axes = set(matrix["axes"])
    bad = {
        c["id"]: [k for k in c if k != "id" and k not in axes]
        for c in matrix["core_cells"]
        + (matrix.get("control_cells") or [])
        + (matrix.get("multi_instance_cells") or [])
    }
    bad = {k: v for k, v in bad.items() if v}
    assert not bad, f"cell(s) key on undeclared axes: {bad}"


@pytest.mark.unit
def test_default_cell_covers_every_axis_a_cell_varies(matrix):
    """Each axis a cell sets must also have a `default_cell` value, or the
    one-axis sweeps have no control value to hold it at."""
    default = set(matrix["default_cell"])
    used = {
        k
        for c in matrix["core_cells"]
        + (matrix.get("control_cells") or [])
        + (matrix.get("multi_instance_cells") or [])
        for k in c
        if k != "id"
    }
    assert not (used - default), (
        f"axes varied by cells but absent from default_cell: {sorted(used - default)}"
    )


@pytest.mark.unit
def test_every_axis_value_in_a_sweep_exists(matrix):
    bad = {}
    for axis, choices in matrix["sweeps"].items():
        if axis not in matrix["axes"]:
            bad[axis] = "axis not declared"
            continue
        known = {str(k).lower() for k in matrix["axes"][axis]}
        # YAML 1.1 turns off/on into booleans on both sides; compare normalized.
        known |= {"off", "on"}
        unknown = [c for c in choices if str(c).lower() not in known]
        if unknown:
            bad[axis] = unknown
    assert not bad, f"sweep(s) name axis values that do not exist: {bad}"


@pytest.mark.unit
@pytest.mark.parametrize(
    "suite,arms",
    [
        ("forcing", ("force-off", "force-on")),
        ("restatement", ("restate-on", "restate-off")),
        ("enforcement", ("enforce-off", "enforce-warn")),
        ("boundary", ("split-llm", "split-disabled")),
    ],
)
def test_the_ab_suites_still_have_both_arms(suites, suite, arms):
    """Named explicitly, so deleting an arm from one of the four feature A/Bs
    fails here with the feature's name rather than passing a generic count check."""
    assert suite in suites, f"suite '{suite}' has been removed"
    for arm in arms:
        assert arm in suites[suite]["cells"], (
            f"suite '{suite}' no longer runs '{arm}' — its comparison is undefined"
        )


@pytest.mark.unit
def test_ab_suites_are_repeated(suites):
    """A single sample cannot resolve a non-deterministic outcome, and every
    feature A/B here is judged on a rate. The matrix's own note says repeats>=3
    for gates; hold the four feature suites to it."""
    for name in ("forcing", "restatement", "enforcement", "boundary"):
        assert int(suites[name].get("repeats", 1)) >= 3, (
            f"suite '{name}' is an A/B judged on rates; repeats must be >= 3"
        )


@pytest.mark.unit
def test_the_guard_is_not_vacuous(matrix):
    """``cells_for_suite`` must RAISE on an unknown cell, not filter it.

    Without this the four tests above are the only thing standing between a typo
    and a one-armed A/B, and a guard whose failure mode is silence is exactly
    what this file exists to remove.
    """
    import make_configs

    fake = {
        "core_cells": matrix["core_cells"],
        "suites": {"bogus": {"cells": ["force-off", "does-not-exist"], "docs": []}},
    }
    with pytest.raises(SystemExit) as exc:
        make_configs.cells_for_suite(fake, "bogus")
    assert "does-not-exist" in str(exc.value)


@pytest.mark.unit
def test_a_known_cell_list_still_resolves(matrix):
    """Guard-the-guard: the raise above must not have broken the happy path."""
    import make_configs

    fake = {
        "core_cells": matrix["core_cells"],
        "suites": {"ok": {"cells": ["force-off", "force-on"], "docs": []}},
    }
    got = make_configs.cells_for_suite(fake, "ok")
    assert [c["id"] for c in got] == ["force-off", "force-on"]


# --------------------------------------------------------------------------- #
# The #653 control arm's frozen prompt.
# --------------------------------------------------------------------------- #
PROMPT_DIR = os.path.join(BENCH, "matrices", "prompts")


@pytest.mark.unit
def test_file_valued_axes_resolve():
    """`boundary_prompt: legacy` names a file; a missing file must be an error,
    not an empty prompt (which Bedrock rejects with "text length 0" halfway
    through a paid run)."""
    import make_configs

    for axis, choices in yaml.safe_load(open(CFG_MATRIX))["axes"].items():
        for choice, knobs in choices.items():
            for dotted, value in (knobs or {}).items():
                if isinstance(value, str) and value.startswith("@file:"):
                    resolved = make_configs._resolve_value(value)
                    assert resolved, f"{axis}.{choice}.{dotted} resolved empty"


@pytest.mark.unit
def test_the_legacy_boundary_prompt_is_actually_the_pre_fix_prompt():
    """The control arm must LACK the fix. If someone "helpfully" refreshes this
    frozen file from the current default, `boundaryctl` becomes two copies of the
    same prompt and its delta silently collapses to zero — which would read as
    "the #653 fix does nothing"."""
    path = os.path.join(PROMPT_DIR, "classification_task_prompt_pre653.txt")
    with open(path) as fh:
        legacy = fh.read()
    assert "boundary-detection-rules" not in legacy, (
        "the frozen pre-#653 prompt contains the fix it is the control for"
    )
    assert "Decide if this page starts a new document" in legacy, (
        "the frozen prompt no longer looks like the pre-#653 wording"
    )


@pytest.mark.unit
def test_the_legacy_prompt_is_still_a_usable_prompt():
    """It is frozen, not inert: it still has to run. Every placeholder the
    classification service substitutes must be present, and the cacheable prefix
    marker must survive, or the control arm fails for reasons unrelated to
    boundary detection."""
    path = os.path.join(PROMPT_DIR, "classification_task_prompt_pre653.txt")
    with open(path) as fh:
        legacy = fh.read()
    for placeholder in (
        "{CLASS_NAMES_AND_DESCRIPTIONS}",
        "{FEW_SHOT_EXAMPLES}",
        "{DOCUMENT_TEXT}",
        "{DOCUMENT_IMAGE}",
        "<<CACHEPOINT>>",
    ):
        assert placeholder in legacy, f"frozen prompt lost {placeholder}"
    assert "document_boundary" in legacy, (
        "frozen prompt no longer asks for document_boundary, so it cannot be "
        "scored on boundary detection at all"
    )


@pytest.mark.unit
def test_control_cells_are_not_in_the_core_grid(matrix):
    """A control arm is a deliberately WRONG configuration. If it also sits in
    ``core_cells`` then `core`, `corefast`, `coresynth` and `full` all run it as
    though it were a configuration a user might choose — which is how a known-
    defective classification prompt (the pre-#653 control) ended up inside the
    release regression grid.
    """
    core = {c["id"] for c in matrix["core_cells"]}
    controls = {c["id"] for c in matrix.get("control_cells") or []} | {
        c["id"] for c in matrix.get("multi_instance_cells") or []
    }
    overlap = core & controls
    assert not overlap, (
        f"cell(s) declared as BOTH a core grid cell and a control arm: {overlap}. "
        f"A control belongs only in control_cells."
    )


@pytest.mark.unit
def test_the_core_grid_expansion_excludes_controls(matrix):
    """The expansion itself, not just the declaration: ``cells: "core_cells"``
    must not yield any control arm."""
    import make_configs

    fake = {
        "core_cells": matrix["core_cells"],
        "control_cells": matrix.get("control_cells") or [],
        "sweeps": matrix["sweeps"],
        "suites": {"grid": {"cells": "core_cells", "docs": []}},
    }
    got = {c["id"] for c in make_configs.cells_for_suite(fake, "grid")}
    controls = {c["id"] for c in fake["control_cells"]}
    assert not (got & controls), (
        f"core_cells expansion leaked controls: {got & controls}"
    )
    assert got, "expansion produced nothing; this test would be vacuous"


@pytest.mark.unit
def test_a_suite_may_still_name_a_control_explicitly(matrix):
    """The control has to remain REACHABLE — `boundaryctl` is the whole reason it
    exists. Removing it from core_cells must not make it unreferenceable."""
    import make_configs

    assert "split-llm-legacyprompt" in [
        c["id"] for c in (matrix.get("control_cells") or [])
    ]
    got = make_configs.cells_for_suite(matrix, "boundaryctl")
    assert [c["id"] for c in got] == ["split-llm-legacyprompt"]


# ---------------------------------------------------------------------------
# #766: a suite naming a reference corpus measured 7 of its 9 documents and
# reported like a clean sweep. run_matrix launches one local PDF per run, the
# reference corpora are test SETS on the stack, and the launch loop `continue`d
# past them under a comment claiming they were "handled separately" — nothing
# handled them. This is the same dangerous shape as the dropped control arm
# above: a benchmark that crashes gets fixed, one that quietly measures less
# gets published.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_reference_docs_have_no_local_pdf_so_they_cannot_be_launched(docm):
    """Pins the premise the reporting depends on.

    If a reference corpus ever DOES get a local PDF, run_matrix could launch it
    and calling it unlaunchable becomes a lie. Note this can only fail on a
    machine that has generated the corpus — ``corpus/docs/`` is gitignored, so on
    a CI checkout the directory is empty and the assertion is trivially true.
    That is fine: the mistake it guards against is made locally.
    """
    docs_dir = os.path.join(BENCH, "corpus", "docs")
    for d in docm.get("reference", []):
        assert not os.path.exists(os.path.join(docs_dir, d["id"] + ".pdf")), (
            f"reference doc {d['id']} now has a local PDF — run_matrix could "
            f"launch it, and the '#766 not launchable' warning is now misleading."
        )


@pytest.mark.unit
def test_core_docs_still_names_the_reference_corpora(docm):
    """The shortfall has to stay *reachable*, or the guard below is vacuous:
    ``core_docs`` is the group most suites use and it is where the 7-of-9
    shortfall happens. If core_docs is ever pointed at synthetic-only documents
    on purpose, delete this test — it pins a status quo, not a goal."""
    core_docs = docm["groups"]["core_docs"]
    reference = {d["id"] for d in docm.get("reference", [])}
    assert reference & set(core_docs), (
        "core_docs no longer names any reference corpus; if that was deliberate, "
        "delete this test — otherwise the group lost its only real-document "
        "coverage."
    )


@pytest.mark.unit
def test_reference_corpora_are_reported_as_unlaunchable_not_dropped(docm):
    """The behavior, not the wording.

    The first attempt at this fix computed the unlaunchable set from a missing
    local PDF *after* the class filter had already removed reference docs (a
    reference doc's "class" is its config, so it never matches ``--class``). The
    set was therefore always empty, the warning could never fire, and
    ``docs_unlaunchable`` recorded ``[]`` on precisely the runs that dropped two
    documents. A source-string test passed anyway; only this catches it.
    """
    import run_matrix

    core_docs = docm["groups"]["core_docs"]
    refs = run_matrix.reference_ids(docm)
    # What load_plan's class filter leaves for a bank_statement grid.
    kept, _ = run_matrix._docs_for_class(core_docs, docm, "bank_statement")

    runnable, unlaunchable, other_class = run_matrix.plan_coverage(
        core_docs, kept, refs
    )

    assert set(unlaunchable) == refs & set(core_docs), (
        f"the reference corpora in core_docs must be reported as unlaunchable; "
        f"got {unlaunchable}"
    )
    assert unlaunchable, "no reference corpus reached the report — guard is vacuous"
    assert not set(runnable) & refs, "a reference corpus was left in the run plan"
    assert not set(other_class) & refs, (
        "a reference corpus was filed as 'another class', which sends the reader "
        "to a --class that cannot run it"
    )
    # Nothing the suite named may vanish from all three buckets.
    assert set(runnable) | set(unlaunchable) | set(other_class) == set(core_docs)


@pytest.mark.unit
def test_a_document_of_another_class_is_not_called_unlaunchable(docm):
    """The two causes are different work: another class is runnable under its own
    ``--class``, a reference corpus is not runnable here at all. Collapsing them
    would put ``kv_form`` in a bucket that says "use Test Studio"."""
    import run_matrix

    docs = ["small_narrow", "kv_form"] + sorted(run_matrix.reference_ids(docm))
    kept, _ = run_matrix._docs_for_class(docs, docm, "bank_statement")
    runnable, unlaunchable, other_class = run_matrix.plan_coverage(
        docs, kept, run_matrix.reference_ids(docm)
    )

    assert runnable == ["small_narrow"]
    assert other_class == ["kv_form"]
    assert set(unlaunchable) == run_matrix.reference_ids(docm)
