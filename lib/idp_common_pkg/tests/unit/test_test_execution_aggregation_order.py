# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Re-aggregating the same per-document results gives the same run metrics (#2133).

Two inputs to the Test Studio aggregation arrive in an order nothing fixes: the
tracking-table scan, and the order the per-document loader threads finish in.
Each test here aggregates one fixed set of results under several such orders and
requires byte-identical output, so a fold that depends on arrival order fails.
"""

import json
import os
import random
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

from idp_common.evaluation.contract import STICKLER_RESULT_VERSION

FUNCTION_PATH = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "../../../../patterns/unified/src/test_execution_aggregation_function",
    )
)

DOC_COUNT = 10


def _import_index():
    if FUNCTION_PATH not in sys.path:
        sys.path.insert(0, FUNCTION_PATH)
    sys.modules.pop("index", None)
    import index

    return index


def _doc_key(i: int) -> str:
    return f"run-1/doc-{i:02d}.pdf"


def _payload(i: int) -> dict:
    """One document's results.json.

    Every document's single field carries the same confidence and every other one
    is wrong, so a review budget that covers some but not all of them has to break
    a tie: which errors count as caught then depends on the order Stickler sees
    the documents in. The weighted and graded scores are thirds, whose float sum
    depends on summation order in the last digits.
    """
    match = i % 2 == 0
    return {
        "stickler_result_version": STICKLER_RESULT_VERSION,
        "section_results": [
            {
                "section_id": "1",
                "stickler_comparison_result": {
                    "field_comparisons": [
                        {
                            "field_path": "Amount",
                            "expected_key": "Amount",
                            "actual_key": "Amount",
                            "expected_value": "10",
                            "actual_value": "10" if match else f"9{i}",
                            "match": match,
                            "score": 1.0 if match else 0.0,
                        }
                    ],
                    "prediction_confidences": {"Amount": 0.5},
                    "confusion_matrix": {
                        "tp": 1 if match else 0,
                        "fp": 0,
                        "tn": 0,
                        "fn": 0,
                        "fd": 0 if match else 1,
                    },
                    "overall_score": 1.0 if match else 0.0,
                },
            }
        ],
        "overall_metrics": {"weighted_overall_score": (i + 1) / 3 / DOC_COUNT},
        "doc_split_metrics": {
            "final_score": (i + 2) / 3 / DOC_COUNT,
            "clustering_score": (i + 1) / 7,
        },
    }


def _orders():
    """(scan order, finish order) pairs: forward, reversed and seeded shuffles."""
    keys = [_doc_key(i) for i in range(DOC_COUNT)]
    rng = random.Random(2133)
    pairs = [(keys, keys), (keys[::-1], keys[::-1]), (keys, keys[::-1])]
    for _ in range(3):
        scan, finish = keys[:], keys[:]
        rng.shuffle(scan)
        rng.shuffle(finish)
        pairs.append((scan, finish))
    return pairs


def _aggregate(index, scan_order, finish_order, run):
    """Run ``run(index)`` with the scan in ``scan_order`` and the loads released in
    ``finish_order``.

    Each load waits until the document before it in ``finish_order`` has been
    loaded. That does not fully decide the order the old ``as_completed`` fold saw
    them in, because the loads take microseconds and futures that are already
    done come back in no particular order, so between the scan order, the release
    order and that, the six orders below reliably differ for an order-dependent
    fold. The wait is bounded so a load that is never started cannot hang the
    suite.
    """
    payloads = {
        key: _payload(i) for i, key in enumerate(_doc_key(i) for i in range(DOC_COUNT))
    }
    done = {key: threading.Event() for key in finish_order}
    previous = {key: finish_order[n - 1] for n, key in enumerate(finish_order) if n}

    def load(uri):
        key = next(k for k in payloads if f"/{k}/" in uri)
        if key in previous:
            done[previous[key]].wait(timeout=5)
        done[key].set()
        return json.loads(json.dumps(payloads[key]))

    table = MagicMock()
    table.scan.return_value = {
        "Items": [
            {
                "PK": f"doc#run-1#{key}",
                "ObjectKey": key,
                "EvaluationStatus": "COMPLETED",
            }
            for key in scan_order
        ]
    }
    with (
        patch.object(index, "dynamodb") as dynamodb,
        patch.object(index, "_load_s3_json", side_effect=load),
    ):
        dynamodb.Table.return_value = table
        return run(index)


@pytest.fixture
def env():
    with patch.dict(
        os.environ,
        {"TRACKING_TABLE": "t", "OUTPUT_BUCKET": "b", "LOG_LEVEL": "WARNING"},
    ):
        yield


@pytest.mark.unit
def test_loaded_results_do_not_depend_on_scan_or_finish_order(env):
    """What the loader hands the aggregation is the same, in the same order."""
    index = _import_index()
    outputs = {
        json.dumps(
            _aggregate(
                index,
                scan,
                finish,
                lambda ix: ix._load_comparison_results("run-1", "t"),
            ),
            default=str,
        )
        for scan, finish in _orders()
    }
    assert len(outputs) == 1, f"{len(outputs)} different loader outputs"


@pytest.mark.unit
def test_run_metrics_do_not_depend_on_scan_or_finish_order(env):
    """The run metrics themselves, through real Stickler, including the tie-sensitive
    error capture at the review budget."""
    pytest.importorskip("stickler")
    index = _import_index()
    results = [
        _aggregate(
            index,
            scan,
            finish,
            lambda ix: ix.aggregate_test_run_with_stickler("run-1", "t"),
        )
        for scan, finish in _orders()
    ]
    for result in results:
        # Stickler's own timing of the aggregation call: wall clock by definition,
        # and not a metric.
        result.pop("total_time")
    first = json.dumps(results[0], default=str)
    for n, result in enumerate(results[1:], start=1):
        assert json.dumps(result, default=str) == first, f"order {n} differs"


@pytest.mark.unit
def test_a_numeric_object_key_does_not_fail_the_run(env):
    """A tracking record can hold a number where ``ObjectKey`` should be a string
    (#2165); ordering the keys must not make that one record fail every other."""
    from decimal import Decimal

    index = _import_index()
    table = MagicMock()
    table.scan.return_value = {
        "Items": [
            {
                "PK": "doc#run-1#a",
                "ObjectKey": _doc_key(0),
                "EvaluationStatus": "COMPLETED",
            },
            {
                "PK": "doc#run-1#b",
                "ObjectKey": Decimal("123"),
                "EvaluationStatus": "COMPLETED",
            },
        ]
    }
    with (
        patch.object(index, "dynamodb") as dynamodb,
        patch.object(index, "_load_s3_json", return_value=_payload(0)),
    ):
        dynamodb.Table.return_value = table
        comparisons, *_ = index._load_comparison_results("run-1", "t")
    loaded = {c["_idp_source"]["doc_key"] for c in comparisons}
    assert loaded == {_doc_key(0), Decimal("123")}
