# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Confidence coverage as a RECORDED benchmark figure (issue #997).

What was missing
----------------
The ``assessment_coverage_incomplete`` guard fires when extracted list rows carry
no confidence — 5% of rows unscored for a warning, 25% plus 10 absolute unscored
rows for an error. Those rungs rest on the expectation that a healthy run sits at
or near 0% shortfall, which is a property read off the reconciliation code path and
was never measured: no benchmark artifact, evaluation report or release-validation
record in this repository carried a scored-rows / extracted-rows figure.

``n_gaps`` next door in ``analyze.score_synthetic`` is easy to mistake for it and
is not: it counts truth ids the extraction never produced. A document can have
``n_gaps == 0`` — every row recovered — and still have every one of those rows
unscored, which is exactly the shape #901 reported.

So ``analyze.score_confidence_coverage`` records the figure per document, and
``aggregate.cell_stats`` rolls up its distribution (min/max/stdev/CV, not only the
mean) per cell.

What these tests pin
--------------------
1. The scorer reads ``explainability_info``'s one-element-list wrapper correctly —
   getting that wrong yields a uniform 0% coverage that looks like a catastrophic
   finding rather than a scorer bug.
2. It uses ``idp_common``'s ``confidence_coverage``, i.e. the rule the guard
   actually fires on, rather than a lookalike.
3. Coverage is ``None`` — not 1.0 — for a document with no list attribute, and
   ``_stats`` therefore excludes it from a corpus mean.
4. The keys reach ``CSV_COLS``. A row key absent from that list is dropped by
   ``DictWriter(extrasaction="ignore")`` in silence, which is how ``n_conf_leaves``
   came to be computed by the scorer and readable from nothing.

Note on where this runs: ``benchmarks/tests`` is in ``scripts/run_all_tests.py``'s
``RUN_ROOTS``, so ``make test`` covers it, and ``make test-packages-cicd`` runs it in
both CIs. ``scripts/tests/test_src_lambda_tests_in_ci.py`` derives the universe of
directories holding a tracked ``test_*.py`` and fails if one reaches neither CI, so
this directory cannot fall out of coverage unnoticed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness_import import harness_module

analyze = harness_module("analyze")
aggregate = harness_module("aggregate")


def _leaf(confidence):
    return {"confidence": confidence, "confidence_reason": "ok"}


def _section(n_rows: int, n_scored: int, *, wrap: bool = True):
    """One section result.json as the harness reads it off S3."""
    assessment = {
        "transactions": [
            {"amount": _leaf(0.94 if i < n_scored else None)} for i in range(n_rows)
        ]
    }
    return {
        "inference_result": {
            "account_number": "1234",
            "transactions": [{"amount": f"{i}.00"} for i in range(n_rows)],
        },
        "explainability_info": [assessment] if wrap else assessment,
    }


def test_fully_scored_document_records_complete_coverage():
    out = analyze.score_confidence_coverage([_section(50, 50)])
    assert out["conf_rows_expected"] == 50
    assert out["conf_rows_scored"] == 50
    assert out["conf_rows_unscored"] == 0
    assert out["conf_coverage"] == 1.0
    assert out["conf_unscored_by_field"] is None


def test_partial_coverage_is_recorded_with_its_per_field_breakdown():
    out = analyze.score_confidence_coverage([_section(100, 90)])
    assert out["conf_rows_expected"] == 100
    assert out["conf_rows_scored"] == 90
    assert out["conf_coverage"] == 0.9
    assert out["conf_unscored_by_field"] == {"transactions": 10}


def test_coverage_sums_across_the_sections_of_one_document():
    """A document is scored as a whole; a packet whose second section is unscored
    must not be averaged into looking half-healthy per section and fine overall."""
    out = analyze.score_confidence_coverage([_section(40, 40), _section(60, 0)])
    assert out["conf_rows_expected"] == 100
    assert out["conf_rows_scored"] == 40
    assert out["conf_coverage"] == 0.4


def test_the_explainability_wrapper_list_is_unwrapped():
    """``explainability_info`` is written as ``[assessment_dict]``. Passing the list
    itself where the dict is expected makes every row look unscored, so a scorer bug
    would masquerade as a 0%-coverage finding across the whole corpus."""
    wrapped = analyze.score_confidence_coverage([_section(30, 30, wrap=True)])
    bare = analyze.score_confidence_coverage([_section(30, 30, wrap=False)])
    assert wrapped == bare
    assert wrapped["conf_coverage"] == 1.0


def test_a_document_with_no_list_attribute_has_undefined_coverage():
    """Not 1.0. A corpus mean that counted these as perfect would be reporting the
    proportion of list-free documents in the corpus."""
    out = analyze.score_confidence_coverage(
        [{"inference_result": {"account_number": "1234"}, "explainability_info": [{}]}]
    )
    assert out["conf_rows_expected"] == 0
    assert out["conf_coverage"] is None
    # And the roll-up drops it rather than averaging a None or a 1.0 in.
    stats = aggregate._stats([1.0, None, 0.5])
    assert stats["n"] == 2
    assert stats["mean"] == 0.75


def test_it_uses_the_shipping_guards_rule_not_a_lookalike():
    """The measurement must be the predicate that fires in production.

    A row is unscored when ANY confidence leaf in it is None — including a leaf
    inside a nested group or an inner list — and a re-implementation in the harness
    would drift from that silently. Asserted by comparing against
    ``idp_common.assessment.batching.confidence_coverage`` on a nested shape, which
    is the function the scorer is required to call.
    """
    from idp_common.assessment.batching import confidence_coverage

    nested_assessment = {
        "records": [
            {
                "Employee": {"Name": _leaf(0.99), "Id": _leaf(1.0)},
                "Earnings": [{"Amount": _leaf(0.98)}],
            },
            {
                "Employee": {"Name": _leaf(0.99), "Id": _leaf(None)},
                "Earnings": [{"Amount": _leaf(0.98)}],
            },
        ]
    }
    data = {
        "records": [
            {"Employee": {"Name": "A", "Id": "1"}, "Earnings": [{"Amount": "1"}]},
            {"Employee": {"Name": "B", "Id": "2"}, "Earnings": [{"Amount": "2"}]},
        ]
    }
    direct = confidence_coverage(nested_assessment, data)
    out = analyze.score_confidence_coverage(
        [{"inference_result": data, "explainability_info": [nested_assessment]}]
    )
    assert out["conf_rows_expected"] == direct["expected_rows"] == 2
    assert out["conf_rows_scored"] == direct["scored_rows"] == 1
    assert out["conf_unscored_by_field"] == direct["unscored_rows_by_field"]


@pytest.mark.parametrize(
    "column",
    ["conf_rows_expected", "conf_rows_scored", "conf_rows_unscored", "conf_coverage"],
)
def test_every_coverage_key_reaches_the_csv(column):
    """``DictWriter(extrasaction="ignore")`` drops an unlisted row key without a
    word. Every key the scorer emits must therefore be declared, or the figure is
    recorded in summary.json and invisible in the artifact most readers open."""
    assert column in aggregate.CSV_COLS


@pytest.mark.parametrize("scorer", ["score_synthetic", "score_reference"])
def test_both_scorers_emit_the_coverage_keys(scorer):
    """Synthetic and reference documents both have to carry the figure.

    The reference path is the one an earlier metric (``rows_extracted``) was added to
    only the synthetic side of — which is exactly why reference-corpus coverage
    cannot be computed from any stored artifact today. Checking only one scorer would
    repeat that.

    Checked by AST rather than by counting a substring in the source: a source-text
    count breaks on a reformat, which is a failure for the wrong reason, and it cannot
    tell which function the call sits in. Calling the scorers for real would need S3
    and DynamoDB.
    """
    import ast

    source = Path(analyze.__file__).with_suffix(".py")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == scorer
        ),
        None,
    )
    assert function is not None, f"{scorer} is gone from {source}"

    splices = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Dict)
        # A `**call(...)` entry in a dict literal has None as its key.
        for key, value in zip(node.keys, node.values)
        if key is None
        and isinstance(value, ast.Call)
        and getattr(value.func, "id", None) == "score_confidence_coverage"
    ]
    assert splices, (
        f"{scorer} does not splice score_confidence_coverage(...) into a returned "
        "dict, so documents scored by that path carry no coverage figure at all — "
        "and a corpus mean over the rest would silently be a mean over a subset."
    )
    assert "conf_coverage" in set(analyze.score_confidence_coverage([_section(3, 3)]))
