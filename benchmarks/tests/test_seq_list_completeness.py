# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""List completeness on the synthetic statements, scored by SEQ tag (EX-08).

Test Studio's evaluation pairs rows by similarity and outruns its time limit on a
complete statement of 1,600 rows or more, so it scores only lists that came back short.
collect_guidance_results.py joins each returned row to its true row by the unique
`SEQnnnnn` tag the generator writes into every description, for every statement that
finished. These pin what completeness and accuracy over the rows returned count, where
the sections are read from, how a run pools its statements, the extraction-only cost,
and that the economics count a completeness gap in rows.
"""

from __future__ import annotations

import io
import json

from harness_import import harness_module

cgr = harness_module("collect_guidance_results")
rg = harness_module("render_guidance")


def row(seq: int, amount: float = -10.0) -> dict:
    return {
        "Date": "2024-01-02",
        "Description": f"SEQ{seq:05d} Shop #{seq}",
        "Amount": amount,
    }


TRUTH_ROWS = [row(n) for n in range(100, 105)]
BASELINE = {
    "inference_result": {
        "Account Number": "000123456789",
        "Transactions": TRUTH_ROWS,
    }
}


def section(rows: list[dict]) -> dict:
    return {
        "inference_result": {"Account Number": "000123456789", "Transactions": rows}
    }


def test_truth_is_each_rows_tag_and_its_other_cells():
    truth = cgr.seq_truth(BASELINE)
    assert truth["list_key"] == "Transactions"
    assert truth["seq_ids"] == [100, 101, 102, 103, 104]
    # The tagged description is the join key, not a cell that is scored.
    assert truth["rows_typed"]["SEQ00100"] == {"Date": "2024-01-02", "Amount": -10.0}


def test_a_list_without_tags_has_no_seq_truth():
    untagged = {"inference_result": {"Items": [{"Description": "Widget", "Amount": 1}]}}
    assert cgr.seq_truth(untagged) is None
    one_untagged = [
        *TRUTH_ROWS,
        {"Date": "2024-01-02", "Description": "x", "Amount": 1},
    ]
    assert cgr.seq_truth({"inference_result": {"Transactions": one_untagged}}) is None


def test_completeness_counts_true_rows_back_once_and_accuracy_their_cells():
    truth = cgr.seq_truth(BASELINE)
    returned = [
        row(100),
        row(101),
        row(101),  # a duplicate is one row back
        {**row(102), "Amount": "-10.00"},  # a number returned as text is a wrong cell
        row(99999),  # a tag that names no true row is not a row back
    ]
    got = cgr.seq_score([section(returned)], truth)
    assert (got.true_rows, got.rows_back) == (5, 3)
    assert (got.cells_right, got.cells) == (5, 6)


def test_rows_split_across_sections_all_count():
    truth = cgr.seq_truth(BASELINE)
    got = cgr.seq_score([section(TRUTH_ROWS[:2]), section(TRUTH_ROWS[2:])], truth)
    assert (got.rows_back, got.cells_right, got.cells) == (5, 10, 10)


class FakeS3:
    """The two S3 calls SeqLists makes, over an in-memory bucket per name."""

    def __init__(self, objects: dict[tuple[str, str], object]) -> None:
        self.objects = objects

    def get_object(self, Bucket: str, Key: str) -> dict:
        return {"Body": io.BytesIO(json.dumps(self.objects[(Bucket, Key)]).encode())}

    def get_paginator(self, _name: str):
        objects = self.objects

        class Pages:
            def paginate(self, Bucket: str, Prefix: str, Delimiter: str = ""):
                keys = [k for b, k in objects if b == Bucket and k.startswith(Prefix)]
                if Delimiter:
                    tops = sorted(
                        {Prefix + k[len(Prefix) :].split("/")[0] + "/" for k in keys}
                    )
                    return [{"CommonPrefixes": [{"Prefix": p} for p in tops]}]
                return [{"Contents": [{"Key": k} for k in keys]}]

        return Pages()


def test_seq_lists_reads_every_section_result_and_nothing_beside_it(tmp_path):
    run, doc = "synth-statements-5-20261120-000000", "synth-statements-5-01.pdf"
    out = f"{run}/{doc}/sections"
    s3 = FakeS3(
        {
            (
                "tsb",
                f"synth-statements-5/baseline/{doc}/sections/1/result.json",
            ): BASELINE,
            ("ob", f"{out}/1/result.json"): section(TRUTH_ROWS[:2]),
            ("ob", f"{out}/2/result.json"): section(TRUTH_ROWS[2:4]),
            ("ob", f"{out}/1/result.json.metadata.json"): {"x": 1},
            ("ob", f"{out}/1/summary.json"): section(TRUTH_ROWS),
            ("ob", f"{run}/{doc}/evaluation/results.json"): {},
        }
    )
    lists = cgr.SeqLists.__new__(cgr.SeqLists)
    lists.s3, lists.output_bucket, lists.testset_bucket = s3, "ob", "tsb"
    lists.cache = tmp_path
    assert lists.documents(run) == [doc]
    got = lists.score(run, "synth-statements-5", doc)
    assert (got.true_rows, got.rows_back) == (5, 4)
    # Cached: a second read needs no S3 at all.
    lists.s3 = None
    assert lists.score(run, "synth-statements-5", doc) == got


class FixedScores:
    def __init__(self, scores: dict) -> None:
        self.scores = scores

    def score(self, _run, _testset, doc):
        return self.scores[doc]


def test_a_run_pools_its_statements_rows():
    scores = {
        "a.pdf": cgr.SeqScore(
            true_rows=800, rows_back=800, cells_right=1600, cells=1600
        ),
        "b.pdf": cgr.SeqScore(true_rows=800, rows_back=43, cells_right=86, cells=86),
    }
    doc_metrics: dict = {}
    per_run: dict = {}
    true_rows: dict = {}
    cgr.score_seq_lists(
        FixedScores(scores),
        "run",
        "ts",
        ["a.pdf", "b.pdf"],
        doc_metrics,
        per_run,
        true_rows,
    )
    assert per_run["completeness"] == [843 / 1600]
    assert per_run["cell_accuracy"] == [1.0]
    assert doc_metrics["b.pdf"]["completeness"] == [43 / 800]
    assert true_rows == {"a.pdf": 800, "b.pdf": 800}


def test_a_statement_with_no_rows_back_has_completeness_but_no_accuracy():
    scores = {"a.pdf": cgr.SeqScore(true_rows=100, rows_back=0, cells_right=0, cells=0)}
    doc_metrics: dict = {}
    per_run: dict = {}
    cgr.score_seq_lists(
        FixedScores(scores), "r", "t", ["a.pdf"], doc_metrics, per_run, {}
    )
    assert per_run == {"completeness": [0.0]}
    assert doc_metrics == {"a.pdf": {"completeness": [0.0]}}


def test_extraction_cost_counts_the_extraction_step_alone():
    metering = {
        "Extraction/bedrock/us.anthropic.claude-haiku-5-5": {"inputTokens": 1_000_000},
        "Assessment/bedrock/us.anthropic.claude-haiku-5-5": {"inputTokens": 1_000_000},
        "OCR/textract/analyze_document-Layout": {"pages": 10},
    }
    steps = cgr.STEP_COSTS["extraction_cost_per_1000_docs"]
    total, extraction = cgr.metering_cost(metering, set(), steps)
    per_million = 1_000_000 * rg.unit_price(
        "bedrock/us.anthropic.claude-haiku-5-5", "inputTokens"
    )
    assert abs(extraction - per_million) < 1e-9
    assert total > 2 * extraction - 1e-9


def test_the_economics_count_a_completeness_gap_in_rows():
    assert rg.error_unit("list", "completeness") == "row"
    assert rg.error_unit("list", "cell_accuracy") == "document"
    assert (
        rg.units_per_doc({"rows_per_doc": {"synth_800": 800.0}}, "row", "synth_800")
        == 800.0
    )
    e = rg.Econ("row", 800.0, "documents", "completeness", " of extraction")
    # 1,000 statements of 800 rows, 60% of rows more: 480,000 rows.
    delta = {"extra": 48.0, "gain": 0.6, "cost_sign": 1, "gain_sign": 1}
    text = rg.econ_sentence("agentic", "single-pass", delta, e)
    assert "480,000 more rows right" in text
    assert "per row it saves" in text
    assert "finding and fixing a missing row" in text


def test_time_stops_where_test_studios_evaluation_starts():
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 11, 20, 19, 19, 26, tzinfo=timezone.utc)

    def at(seconds: int, **event) -> dict:
        return {"timestamp": t0 + timedelta(seconds=seconds), **event}

    # Newest first, as the history is read; evaluation ran 6 minutes of the 13.
    events = [
        at(774, type="ExecutionSucceeded"),
        at(
            773,
            type="PassStateEntered",
            stateEnteredEventDetails={"name": "WorkflowComplete"},
        ),
        at(
            427,
            type="TaskStateEntered",
            stateEnteredEventDetails={"name": "EvaluationStep"},
        ),
        at(
            404,
            type="TaskStateEntered",
            stateEnteredEventDetails={"name": "SummarizationStep"},
        ),
    ]
    assert cgr.seconds_before_evaluation(t0, events) == 427.0
    # No evaluation step: the whole workflow. Still running: no figure yet.
    assert cgr.seconds_before_evaluation(t0, events[:2]) == 774.0
    assert cgr.seconds_before_evaluation(t0, events[3:]) is None


def test_each_list_length_gets_a_band_of_its_own():
    series = [
        ("a", [("x", 100.0, 1.0), ("y", 400.0, 1.0)]),
        ("b", [("z", 3200.0, 0.0)]),
    ]
    bands = rg.size_bands("each", series)
    gp = harness_module("guidance_plots")
    assert gp.band_names(bands, "rows") == [
        "100",
        "101–399",
        "400",
        "401–3,199",
        "3,200",
        "More than 3,200",
    ]
    assert [gp._band_index(x, bands) for x in (100, 400, 3200)] == [0, 2, 4]
    assert rg.size_bands([0, 1, 21], series) == [0, 1, 21]
    assert rg.size_bands(None, series) is None


def test_dots_are_sized_by_the_cost_the_page_shows():
    gp = harness_module("guidance_plots")
    ab = {"cost_metric": "extraction_cost_per_1000_docs"}
    with_step = {"d|v": {"a.pdf": {"cost": 0.85, "extraction_cost_per_1000_docs": 8.5}}}
    assert gp.dot_cost_key(ab, with_step) == "extraction_cost_per_1000_docs"
    # A results file collected before the step cost was written keeps the whole cost.
    assert gp.dot_cost_key(ab, {"d|v": {"a.pdf": {"cost": 0.85}}}) == "cost"
    assert gp.dot_cost_key({}, with_step) == "cost"


def test_a_value_bedrock_refused_reads_not_sent_not_failed():
    datasets = {
        "synth_3200": {
            "name": "Statement",
            "short": "Statement, 3,200 rows",
            "kind": "list",
        }
    }
    found = {"documents": 15, "runs": 3, "error": cgr.NOT_SENT_ERROR}
    line = rg.not_sent_line(
        {"synth_3200|Single-pass": found}, ["synth_3200"], ["Single-pass"], datasets
    )
    assert line.startswith(
        "Not sent: Statement, 3,200 rows — Single-pass: Amazon Bedrock refused"
    )
    assert "all 3 runs (15 in all)" in line and "`ExtractionInputTooLarge`" in line
    assert rg.not_sent_line({}, ["synth_3200"], ["Single-pass"], datasets) == ""
    # A not-sent cell is a string: printed as it is and never a column's winner.
    assert rg.cell_text(cgr.NOT_SENT, "completeness") == "Not sent"
    assert (
        rg.winner({"a": cgr.NOT_SENT, "b": {"mean": 1.0, "se": 0.0}}, "completeness")
        is None
    )
