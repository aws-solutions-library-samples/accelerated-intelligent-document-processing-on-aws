# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#766: reference corpora are launched through the TestRunner and scored per
document, instead of being dropped from a suite.

Behavioral tests over the pure pieces — the plan split, the completion rule and
the per-document expansion — because the previous fix's first attempt computed
its set from an input that was always empty and only a behavioral test caught it.
"""

from __future__ import annotations

import os
import sys

import pytest

yaml = pytest.importorskip("yaml")
pytest.importorskip("boto3")

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(BENCH, "harness")
sys.path.insert(0, HARNESS)

import aggregate  # noqa: E402
import run_matrix  # noqa: E402
from make_configs import BASE_CONFIG  # noqa: E402

DOC_MATRIX = os.path.join(BENCH, "matrices", "doc_matrix.yaml")


@pytest.fixture(scope="module")
def docm():
    with open(DOC_MATRIX) as fh:
        return yaml.safe_load(fh)


def test_every_reference_corpus_names_a_class_make_configs_can_build(docm):
    """The launcher builds a corpus's cells with `make_configs --class <class>`,
    so the class must be a BASE_CONFIG key or the remedy it prints cannot work."""
    for spec in docm["reference"]:
        assert spec.get("class") in BASE_CONFIG, spec
        assert spec.get("testset") and int(spec.get("n", 0)) >= 1


def test_reference_plan_splits_on_the_index_not_on_a_pdf(docm):
    specs = run_matrix.reference_specs(docm)
    built = {"realkie"}

    def index_for(spec):
        path = f"/idx/{spec['id']}.yaml"
        return path, (
            [{"cell": "c1", "version": "v", "resolved": {}}]
            if spec["id"] in built
            else None
        )

    launchable, missing = run_matrix.reference_plan(
        ["tiny_form", "realkie", "ocr_bench"], specs, index_for
    )
    assert set(launchable) == {"realkie"}
    assert launchable["realkie"]["spec"]["testset"] == "realkie-fcc-verified"
    assert missing == {"ocr_bench": "/idx/ocr_bench.yaml"}
    # a synthetic doc is not a reference doc and is not reported either way
    assert "tiny_form" not in launchable and "tiny_form" not in missing


def test_reference_docs_pass_the_class_filter_by_their_declared_class(docm):
    """`bank_real` is a bank_statement-class corpus; the class filter must keep it
    under --class bank_statement so plan_coverage can hand it to the launcher,
    and must still file the RealKIE corpus as another class."""
    keep, other = run_matrix._docs_for_class(
        ["tiny_form", "bank_real", "realkie"], docm, "bank_statement"
    )
    assert "bank_real" in keep and "realkie" in other
    # ...and plan_coverage must then pull it out of the synthetic list BEFORE the
    # missing-PDF check, or a bank_real run exits on "no PDF for ['bank_real']".
    runnable, refs, other_class = run_matrix.plan_coverage(
        ["tiny_form", "bank_real", "realkie"], keep, run_matrix.reference_ids(docm)
    )
    assert runnable == ["tiny_form"]
    assert sorted(refs) == ["bank_real", "realkie"]
    assert other_class == []


def test_reference_index_path_uses_the_corpus_class_and_the_same_override_slug(docm):
    spec = run_matrix.reference_specs(docm)["ocr_bench"]
    p = run_matrix.reference_index_path("core", spec, ["extraction_model=sonnet5"])
    p_plain = run_matrix.reference_index_path("core", spec)
    assert os.path.basename(p).startswith("_index_core_ocr_bench")
    assert p != p_plain, "--set variants must not share an index"


@pytest.mark.parametrize(
    "done,failed,expected,complete",
    [
        (1, 0, 1, True),
        (0, 1, 1, True),
        (1, 0, 20, False),
        (19, 0, 20, False),
        (18, 2, 20, True),
        (0, 0, None, False),
    ],
)
def test_run_complete_waits_for_every_document(done, failed, expected, complete):
    st = {"obj_done": done, "failed": failed}
    assert run_matrix.run_complete(st, expected) is complete


def test_reference_run_expands_to_one_scored_row_per_document():
    res = {"output_bucket": "b", "tracking_table": "t"}
    r = {
        "cell": "c1",
        "doc": "realkie",
        "repeat": 0,
        "resolved": {"m": "x"},
        "run_id": "run-1",
        "reference": True,
        "n_docs": 3,
    }
    prefixes = ["run-1/doc-b/", "run-1/doc-a/", "run-1/doc-c/"]
    seen = []

    def score(bucket, tracking, run_id, name, truth):
        seen.append((run_id, name, truth))
        # analyze.score_doc reports the scored file under "doc" — it must not
        # displace the corpus id the row is keyed on.
        return {
            "doc": name,
            "status": "COMPLETED",
            "success": True,
            "weighted_accuracy": 0.5,
        }

    rows = aggregate.score_reference_run(
        res, r, list_prefixes=lambda b, rid: prefixes, score=score
    )
    assert [row["sub_doc"] for row in rows] == ["doc-a", "doc-b", "doc-c"]
    assert all(row["doc"] == "realkie" and row["cell"] == "c1" for row in rows)
    assert all(t is None for _, _, t in seen), "no local truth: score_reference path"
    assert "coverage_note" not in rows[0]


def test_reference_run_reports_missing_documents_and_no_documents():
    res = {"output_bucket": "b", "tracking_table": "t"}
    r = {
        "cell": "c1",
        "doc": "ocr_bench",
        "repeat": 0,
        "run_id": "run-2",
        "reference": True,
        "n_docs": 20,
    }
    rows = aggregate.score_reference_run(
        res,
        r,
        list_prefixes=lambda b, rid: ["run-2/only/"],
        score=lambda *a: {"status": "COMPLETED", "success": True},
    )
    assert rows[0]["coverage_note"] == "1 of 20 documents found"
    empty = aggregate.score_reference_run(
        res, r, list_prefixes=lambda b, rid: [], score=lambda *a: {}
    )
    assert empty == [{**aggregate._key(r), "status": "NO_DOCS", "success": False}]


def test_synthetic_rows_still_carry_the_key_shape():
    k = aggregate._key(
        {"cell": "c", "doc": "d", "repeat": 2, "resolved": {}, "run_id": "r"}
    )
    assert k["sub_doc"] is None and k["repeat"] == 2
    assert "sub_doc" in aggregate.CSV_COLS


def _row(cell, doc, sub_doc, acc, repeat=0):
    return {
        "cell": cell,
        "doc": doc,
        "sub_doc": sub_doc,
        "repeat": repeat,
        "success": True,
        "weighted_accuracy": acc,
    }


def test_release_comparison_pairs_reference_rows_per_document():
    """Twenty documents of one corpus must pair document-by-document, not collapse
    onto the corpus key (last one wins) or pool into a cross-document spread."""
    base = {"rows": [_row("c", "realkie", "a", 0.5), _row("c", "realkie", "b", 0.9)]}
    cur = {"rows": [_row("c", "realkie", "a", 0.6), _row("c", "realkie", "b", 0.9)]}
    deltas = aggregate._paired_quality_deltas(cur, base)
    key = ("c", "weighted_accuracy")
    assert key in deltas and sorted(round(d, 3) for d in deltas[key]) == [0.0, 0.1]
    groups = aggregate._by_cell_doc(cur["rows"])
    assert set(groups) == {"c|realkie|a", "c|realkie|b"}
    # synthetic rows keep their two-part key
    assert set(aggregate._by_cell_doc([_row("c", "tiny_form.pdf", None, 1.0)])) == {
        "c|tiny_form.pdf"
    }
