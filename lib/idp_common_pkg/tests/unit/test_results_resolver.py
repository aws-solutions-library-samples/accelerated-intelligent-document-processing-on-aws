# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0


import gzip
import importlib.util
import json
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

# Mock boto3 before importing the Lambda module to prevent NoRegionError
# The Lambda creates boto3 clients at module level which requires AWS region
with patch("boto3.resource") as mock_resource, patch("boto3.client") as mock_client:
    mock_resource.return_value = Mock()
    mock_client.return_value = Mock()

    # Import the specific lambda module using importlib to avoid conflicts
    spec = importlib.util.spec_from_file_location(
        "results_index",
        os.path.join(
            os.path.dirname(__file__),
            "../../../../nested/api-resolvers/src/lambda/test_results_resolver/index.py",
        ),
    )
    if spec is None or spec.loader is None:
        raise ImportError("Could not load test_results_resolver module")
    index = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(index)


@pytest.mark.unit
def test_get_test_results_structure():
    """Test test results data structure"""
    test_run_id = "test-run-123"
    metadata = {
        "TestSetName": "lending-test",
        "Status": "COMPLETE",
        "FilesCount": 2,
        "CompletedFiles": 2,
        "FailedFiles": 0,
        "CreatedAt": "2025-01-01T00:00:00Z",
    }

    result = {
        "testRunId": test_run_id,
        "testSetName": metadata.get("TestSetName"),
        "status": metadata.get("Status"),
        "totalFiles": metadata.get("FilesCount", 0),
        "completedFiles": metadata.get("CompletedFiles", 0),
        "failedFiles": metadata.get("FailedFiles", 0),
        "overallAccuracy": 85.5,
        "averageConfidence": 78.2,
        "accuracyBreakdown": {
            "precision": 0.95,
            "recall": 0.90,
            "f1_score": 0.925,
            "false_alarm_rate": 0.05,
            "false_discovery_rate": 0.03,
        },
        "totalCost": 12.45,
        "createdAt": metadata.get("CreatedAt"),
    }

    assert result["testRunId"] == "test-run-123"
    assert result["testSetName"] == "lending-test"
    assert result["status"] == "COMPLETE"
    assert result["totalFiles"] == 2
    assert result["accuracyBreakdown"]["precision"] == 0.95
    assert result["accuracyBreakdown"]["f1_score"] == 0.925


# NOTE: These tests are commented out as they test the old Parquet-based cost retrieval
# which has been replaced with Athena-based queries in the test_results_resolver Lambda

# @pytest.mark.unit
# @patch.dict(os.environ, {"REPORTING_BUCKET": "test-bucket"})
# @patch("boto3.client")
# @patch("pyarrow.parquet.read_table")
# @patch("pyarrow.fs.S3FileSystem")
# @patch("pyarrow.compute.equal")
# def test_get_document_costs_from_parquet_success(
#     mock_pc_equal, mock_s3fs, mock_read_table, mock_boto3
# ):
#     """Test successful Parquet cost retrieval"""
#     pass

# @pytest.mark.unit
# @patch.dict(os.environ, {"REPORTING_BUCKET": "test-bucket"})
# @patch("boto3.client")
# def test_get_document_costs_no_files_found(mock_boto3):
#     """Test when no Parquet files are found"""
#     pass

# @pytest.mark.unit
# @patch.dict(os.environ, {"REPORTING_BUCKET": ""})
# def test_get_document_costs_no_bucket():
#     """Test when REPORTING_BUCKET is not set"""
#     pass


@pytest.mark.unit
def test_accuracy_breakdown_structure():
    """Test accuracy breakdown data structure"""
    accuracy_breakdown = {
        "precision": 0.95,
        "recall": 0.90,
        "f1_score": 0.925,
        "false_alarm_rate": 0.05,
        "false_discovery_rate": 0.03,
    }

    # Verify all expected metrics are present
    expected_metrics = [
        "precision",
        "recall",
        "f1_score",
        "false_alarm_rate",
        "false_discovery_rate",
    ]
    for metric in expected_metrics:
        assert metric in accuracy_breakdown
        assert isinstance(accuracy_breakdown[metric], float)
        assert 0 <= accuracy_breakdown[metric] <= 1


@pytest.mark.unit
def test_get_test_run_status_evaluating():
    """Test test run status with EVALUATING state"""
    test_run_status = {
        "testRunId": "test-run-456",
        "status": "EVALUATING",
        "filesCount": 3,
        "completedFiles": 2,
        "failedFiles": 0,
        "evaluatingFiles": 1,
        "progress": 66.7,
    }

    assert test_run_status["status"] == "EVALUATING"
    assert test_run_status["completedFiles"] == 2
    assert test_run_status["evaluatingFiles"] == 1
    assert test_run_status["progress"] == 66.7


@pytest.mark.unit
def test_get_test_run_status_partial_complete():
    """Test test run status with PARTIAL_COMPLETE state"""
    test_run_status = {
        "testRunId": "test-run-789",
        "status": "PARTIAL_COMPLETE",
        "filesCount": 5,
        "completedFiles": 3,
        "failedFiles": 2,
        "evaluatingFiles": 0,
        "progress": 60.0,
    }

    assert test_run_status["status"] == "PARTIAL_COMPLETE"
    assert test_run_status["completedFiles"] == 3
    assert test_run_status["failedFiles"] == 2
    assert test_run_status["evaluatingFiles"] == 0
    assert test_run_status["progress"] == 60.0


@pytest.mark.unit
def test_compare_test_runs_structure():
    """Test test run comparison structure"""
    results = {
        "run-1": {"overall_accuracy": 85.5, "total_cost": 12.45},
        "run-2": {"overall_accuracy": 90.2, "total_cost": 15.30},
    }

    metrics_comparison = [
        {
            "metric": "Overall Accuracy",
            "values": {
                k: f"{v.get('overall_accuracy', 0)}%" for k, v in results.items()
            },
        },
        {
            "metric": "Total Cost",
            "values": {k: f"${v.get('total_cost', 0)}" for k, v in results.items()},
        },
    ]

    assert len(metrics_comparison) == 2
    assert metrics_comparison[0]["values"]["run-1"] == "85.5%"
    assert metrics_comparison[1]["values"]["run-2"] == "$15.3"


@pytest.mark.unit
def test_build_comparator_diff_flags_source_change():
    """A leaf that stayed on the same comparator+threshold but flipped from
    operator-configured to auto-inferred (the operator removed the
    annotation and Stickler's native inference now decides) is a real
    change even though the numbers are the same. The panel must show it.
    """
    runs = {
        "run-a": {
            "invoice_id": {
                "comparator": "ExactComparator",
                "threshold": 1.0,
                "source": "configured",
                "why": None,
            }
        },
        "run-b": {
            "invoice_id": {
                "comparator": "ExactComparator",
                "threshold": 1.0,
                "source": "auto-inferred",
                "why": ["name-token:invoice_id -> ExactComparator@1.0"],
            }
        },
    }
    diff = index._build_comparator_diff(runs)
    assert len(diff) == 1
    assert diff[0]["attribute"] == "invoice_id"
    assert diff[0]["entries"]["run-a"]["source"] == "configured"
    assert diff[0]["entries"]["run-b"]["source"] == "auto-inferred"


@pytest.mark.unit
def test_build_comparator_diff_ignores_identical_signatures():
    """When every leaf's (comparator, threshold, source) triple agrees
    across runs the diff is empty and the panel stays hidden. ``why``
    variance alone must not surface a row — the trace is informational
    and its phrasing can differ without implying a scoring change.
    """
    runs = {
        "run-a": {
            "amount": {
                "comparator": "NumericComparator",
                "threshold": 0.95,
                "source": "auto-inferred",
                "why": ["name-token:amount -> NumericComparator@0.95"],
            }
        },
        "run-b": {
            "amount": {
                "comparator": "NumericComparator",
                "threshold": 0.95,
                "source": "auto-inferred",
                # Different phrasing, same decision — must not trigger a row.
                "why": ["type:float -> NumericComparator@0.95"],
            }
        },
    }
    assert index._build_comparator_diff(runs) == []


@pytest.mark.unit
def test_build_comparator_diff_flags_missing_side():
    """An attribute present in only one run is schema-shape drift — surface
    it alongside comparator drift so the operator sees BOTH kinds of
    change in one panel."""
    runs = {
        "run-a": {
            "new_field": {
                "comparator": "LevenshteinComparator",
                "threshold": 0.7,
                "source": "auto-inferred",
            }
        },
        "run-b": {},
    }
    diff = index._build_comparator_diff(runs)
    assert len(diff) == 1
    assert diff[0]["entries"]["run-a"]["comparator"] == "LevenshteinComparator"
    assert diff[0]["entries"]["run-b"] is None


@pytest.mark.unit
def test_build_comparator_diff_needs_two_runs():
    """Diff over one run (or zero) is meaningless — return empty."""
    assert index._build_comparator_diff({"only-run": {"x": {"comparator": "X"}}}) == []
    assert index._build_comparator_diff({}) == []


@pytest.mark.unit
def test_build_comparator_diff_ignores_source_on_cross_version_compare():
    """Runs written before STICKLER_RESULT_VERSION 3.0 had no
    ``inference_source`` field. Including ``source`` in the diff signature
    unconditionally would flip every attribute to "changed" purely because
    one side reads ``None`` and the other reads ``"configured"`` / ``"auto-
    inferred"`` — drowning the panel in false rows during an upgrade
    window. The signature must omit the source axis whenever any entry
    lacks a source, so real comparator/threshold drift remains visible
    while the pseudo-drift of a missing field is suppressed.
    """
    runs = {
        "old-run": {
            "invoice_id": {
                "comparator": "ExactComparator",
                "threshold": 1.0,
                "source": None,  # pre-3.0 results.json — no inference_source
            }
        },
        "new-run": {
            "invoice_id": {
                "comparator": "ExactComparator",
                "threshold": 1.0,
                "source": "auto-inferred",
            }
        },
    }
    diff = index._build_comparator_diff(runs)
    assert diff == [], (
        "Cross-version compare with identical (comparator, threshold) must "
        "not report a change purely because one side has no source"
    )


@pytest.mark.unit
def test_iter_completed_doc_keys_is_deterministic():
    """Sample-doc selection must be deterministic so two runs of the same
    test set converge on the same representative document (otherwise the
    Comparator Changes panel reports one-sided drift purely because the
    sampler picked differently-shaped docs).

    Implementation reads ``Files`` from ``testrun#{id}`` metadata and
    ``batch_get_item``s the ``doc#{run_id}/{file_name}`` rows — the
    earlier unbounded ``Scan`` version exceeded the 29s API Gateway
    ceiling on mature tracking tables. The ``Files`` list is sorted
    before iteration to give the same determinism the Scan version
    obtained by sorting Scan results.
    """
    fake_client = Mock()
    # testrun#{id} metadata read — ``Files`` in reverse-lex order to prove
    # the sampler sorts before iterating
    fake_client.get_item.return_value = {
        "Item": {
            "Files": {
                "L": [
                    {"S": "zeta.pdf"},
                    {"S": "alpha.pdf"},
                    {"S": "mu.pdf"},
                    {"S": "skip-me.pdf"},
                ]
            }
        }
    }
    # doc# BatchGetItem — one entry is FAILED and must be skipped
    fake_client.batch_get_item.return_value = {
        "Responses": {
            "T": [
                {
                    "ObjectKey": {"S": "runid/alpha.pdf"},
                    "EvaluationStatus": {"S": "COMPLETED"},
                },
                {
                    "ObjectKey": {"S": "runid/mu.pdf"},
                    "EvaluationStatus": {"S": "COMPLETED"},
                },
                {
                    "ObjectKey": {"S": "runid/skip-me.pdf"},
                    "EvaluationStatus": {"S": "FAILED"},
                },
                {
                    "ObjectKey": {"S": "runid/zeta.pdf"},
                    "EvaluationStatus": {"S": "COMPLETED"},
                },
            ]
        }
    }
    with (
        patch.dict(os.environ, {"TRACKING_TABLE": "T"}),
        patch.object(index, "ddb_bounded", fake_client),
    ):
        keys = list(index._iter_completed_doc_keys("runid", limit=3))
    assert keys == ["runid/alpha.pdf", "runid/mu.pdf", "runid/zeta.pdf"], (
        "Sample doc selection must be lexicographically deterministic so "
        "two runs of the same test set pick the same representative doc"
    )


@pytest.mark.unit
def test_iter_completed_doc_keys_accepts_files_stored_as_string_set():
    """The test_runner writes ``Files`` as a Python list (DDB ``L`` type)
    via the resource client, but some legacy runs and manual DDB imports
    stored it as a string set (``SS`` type). Reading only the ``L`` shape
    would silently blank the Comparator Changes panel for those runs
    with no visible cause — accept both shapes.
    """
    fake_client = Mock()
    # ``SS`` (string set) shape rather than the ``L`` shape the fixture uses.
    fake_client.get_item.return_value = {
        "Item": {
            "Files": {"SS": ["zeta.pdf", "alpha.pdf", "mu.pdf"]},
        }
    }
    fake_client.batch_get_item.return_value = {
        "Responses": {
            "T": [
                {
                    "ObjectKey": {"S": "runid/alpha.pdf"},
                    "EvaluationStatus": {"S": "COMPLETED"},
                },
                {
                    "ObjectKey": {"S": "runid/mu.pdf"},
                    "EvaluationStatus": {"S": "COMPLETED"},
                },
                {
                    "ObjectKey": {"S": "runid/zeta.pdf"},
                    "EvaluationStatus": {"S": "COMPLETED"},
                },
            ]
        }
    }
    with (
        patch.dict(os.environ, {"TRACKING_TABLE": "T"}),
        patch.object(index, "ddb_bounded", fake_client),
    ):
        keys = list(index._iter_completed_doc_keys("runid", limit=3))
    assert keys == ["runid/alpha.pdf", "runid/mu.pdf", "runid/zeta.pdf"]


@pytest.mark.unit
def test_iter_completed_doc_keys_dedupes_files_before_batch_get():
    """DynamoDB rejects a ``BatchGetItem`` request that contains
    duplicate keys with a ``ValidationException`` — so a ``Files`` list
    with any repeated entry (from a re-upload without cleanup, or a
    manual DDB edit) used to fail the entire request and reduce the
    Comparator Changes panel to empty for the run. The sampler now
    dedupes ``Files`` before building the batch keys.
    """
    fake_client = Mock()
    # Deliberate duplicate — ``alpha.pdf`` appears twice.
    fake_client.get_item.return_value = {
        "Item": {
            "Files": {
                "L": [
                    {"S": "alpha.pdf"},
                    {"S": "alpha.pdf"},
                    {"S": "beta.pdf"},
                ]
            }
        }
    }
    fake_client.batch_get_item.return_value = {
        "Responses": {
            "T": [
                {
                    "ObjectKey": {"S": "runid/alpha.pdf"},
                    "EvaluationStatus": {"S": "COMPLETED"},
                },
                {
                    "ObjectKey": {"S": "runid/beta.pdf"},
                    "EvaluationStatus": {"S": "COMPLETED"},
                },
            ]
        }
    }
    with (
        patch.dict(os.environ, {"TRACKING_TABLE": "T"}),
        patch.object(index, "ddb_bounded", fake_client),
    ):
        keys = list(index._iter_completed_doc_keys("runid", limit=5))

    # Only one BatchGetItem call, with distinct keys — no duplicate keys
    # were passed to DDB even though the Files list had a repeat.
    assert fake_client.batch_get_item.call_count == 1
    submitted_keys = fake_client.batch_get_item.call_args.kwargs["RequestItems"]["T"][
        "Keys"
    ]
    submitted_pks = [k["PK"]["S"] for k in submitted_keys]
    assert submitted_pks == list(dict.fromkeys(submitted_pks)), (
        "Deduped submission — DDB rejects duplicate keys in one batch"
    )
    assert keys == ["runid/alpha.pdf", "runid/beta.pdf"]


@pytest.mark.unit
def test_batch_get_test_run_items_retries_unprocessed_keys():
    """``getTestRuns`` was timing out at the AppSync 20s resolver ceiling
    on any stack that had accumulated a few hundred test runs, because the
    per-batch BatchGetItem loop was sequential AND dropped
    ``UnprocessedKeys`` silently. The retry loop must re-issue unprocessed
    keys until they resolve (or the retry budget is exhausted) — otherwise
    a throttled batch under load returns a shorter test-run list than the
    GSI actually contains.
    """
    # Marshalling client returns unmarshalled responses (bare strings,
    # not typed AttributeValue dicts) and its request keys are untyped
    # too. Mock responses match that shape.
    responses = [
        {
            "Responses": {"T": [{"PK": "testrun#a"}]},
            "UnprocessedKeys": {"T": {"Keys": [{"PK": "testrun#b", "SK": "metadata"}]}},
        },
        {
            "Responses": {"T": [{"PK": "testrun#b"}]},
            "UnprocessedKeys": {},
        },
    ]
    fake_client = Mock()
    fake_client.batch_get_item.side_effect = responses
    # ``_batch_get_test_run_items`` uses the MARSHALLING variant of the
    # bounded client so untyped keys from ``table.query()`` and
    # unmarshalled response reads (``item["TestRunId"]`` as a bare string)
    # both work. Patching the marshalling client — the direct
    # ``ddb_bounded`` is used by the other DDB path.
    with patch.object(index, "ddb_bounded_marshalling", fake_client):
        # Untyped keys — exactly what the caller passes in prod (from
        # ``table.query()`` in ``_query_test_runs_from_gsi``).
        keys = [
            {"PK": "testrun#a", "SK": "metadata"},
            {"PK": "testrun#b", "SK": "metadata"},
        ]
        items = index._batch_get_test_run_items(keys, "T")
    assert fake_client.batch_get_item.call_count == 2, (
        "UnprocessedKeys must be re-issued rather than silently dropped"
    )
    assert {item["PK"] for item in items} == {"testrun#a", "testrun#b"}


@pytest.mark.unit
def test_get_test_runs_clamps_max_items():
    """``getTestRuns`` accepts a caller-supplied ``maxItems`` and must
    clamp it to the server-side hard ceiling. Passing a huge value must
    not translate into 500-key BatchGetItem requests that reintroduce
    the throttle-amplification we removed; passing 0 or negative must
    become at least 1 (a zero-limit DDB Query is a no-op that still
    burns an invocation).
    """
    fake_table = Mock()
    fake_table.table_name = "T"
    fake_table.query.return_value = {"Items": []}
    fake_table.scan.return_value = {"Items": []}
    ceiling = index._GET_TEST_RUNS_ABSOLUTE_MAX

    with (
        patch.dict(os.environ, {"TRACKING_TABLE": "T"}),
        patch.object(index.dynamodb, "Table", return_value=fake_table),
    ):
        index.get_test_runs(
            "2026-01-01T00:00:00Z", "2026-01-08T00:00:00Z", max_items=999
        )
        # First Query's Limit is the clamped value, not the raw 999.
        assert fake_table.query.call_args.kwargs["Limit"] == ceiling

        fake_table.query.reset_mock()
        index.get_test_runs("2026-01-01T00:00:00Z", "2026-01-08T00:00:00Z", max_items=0)
        assert fake_table.query.call_args.kwargs["Limit"] == 1

        fake_table.query.reset_mock()
        # None means "use the server default".
        index.get_test_runs(
            "2026-01-01T00:00:00Z", "2026-01-08T00:00:00Z", max_items=None
        )
        assert fake_table.query.call_args.kwargs["Limit"] == ceiling

        fake_table.query.reset_mock()
        # Malformed strings fall back to the ceiling (defensive).
        index.get_test_runs(
            "2026-01-01T00:00:00Z", "2026-01-08T00:00:00Z", max_items="not-a-number"
        )
        assert fake_table.query.call_args.kwargs["Limit"] == ceiling


@pytest.mark.unit
def test_load_sample_attribute_methods_swallows_read_timeout():
    """The panel is a UI nicety — it must NOT fault compare_test_runs on
    a transient S3 hiccup. ``botocore.exceptions.ReadTimeoutError`` is a
    subclass of ``BotoCoreError``, NOT ``ClientError``, so an earlier
    ``except (ClientError, ValueError, KeyError)`` let timeouts escape.
    Broadened to ``except Exception`` — pin the contract here.
    """
    from botocore.exceptions import ReadTimeoutError

    fake_s3 = Mock()
    fake_s3.get_object.side_effect = ReadTimeoutError(endpoint_url="http://x")
    with (
        patch.dict(os.environ, {"OUTPUT_BUCKET": "b", "TRACKING_TABLE": "T"}),
        patch.object(index, "s3_bounded", fake_s3),
        patch.object(
            index, "_iter_completed_doc_keys", return_value=iter(["runid/doc1.pdf"])
        ),
    ):
        # Must return {} rather than propagating ReadTimeoutError.
        assert index._load_sample_attribute_methods("runid") == {}


@pytest.mark.unit
def test_load_sample_attribute_methods_uses_document_class_key():
    """Diff key is ``{document_class}.{attribute_name}`` — NOT
    ``{section_id}.{attribute_name}`` (positional; false drift across
    differently-sectioned docs) and NOT the bare attribute name (collides
    across classes in a multi-class packet, hiding real cross-class
    differences). Same-class sections share a schema and legitimately
    collapse; different-class sections stay distinct.
    """
    fake_s3 = Mock()

    class _Body:
        # Real botocore StreamingBody has ``.close()``; the resolver now
        # calls it explicitly to release the urllib3 connection promptly
        # under the parallel fanout in ``compare_test_runs``.
        def close(self):
            pass

        def read(self):
            return json.dumps(
                {
                    "section_results": [
                        {
                            "section_id": "2",
                            "document_class": "Invoice",
                            "attributes": [
                                {
                                    "name": "Amount",
                                    "comparator_type": "NumericComparator",
                                    "evaluation_threshold": 0.95,
                                    "inference_source": "auto-inferred",
                                    "inference_why": ["name-token"],
                                }
                            ],
                        },
                        # Different class, same-named attribute — MUST stay
                        # distinct in the diff (different schemas can carry
                        # different comparators).
                        {
                            "section_id": "3",
                            "document_class": "Receipt",
                            "attributes": [
                                {
                                    "name": "Amount",
                                    "comparator_type": "NumericComparator",
                                    "evaluation_threshold": 0.99,
                                    "inference_source": "configured",
                                    "inference_why": None,
                                }
                            ],
                        },
                    ]
                }
            ).encode()

    fake_s3.get_object.return_value = {"Body": _Body()}
    with (
        patch.dict(os.environ, {"OUTPUT_BUCKET": "b", "TRACKING_TABLE": "T"}),
        patch.object(index, "s3_bounded", fake_s3),
        patch.object(
            index, "_iter_completed_doc_keys", return_value=iter(["runid/doc.pdf"])
        ),
    ):
        methods = index._load_sample_attribute_methods("runid")
    # Two entries — one per class — NOT one entry with the last-write
    # winning. If this collapses to a single ``Amount`` key, the diff
    # can't tell that ``Invoice.Amount`` and ``Receipt.Amount`` diverge.
    assert set(methods.keys()) == {"Invoice.Amount", "Receipt.Amount"}
    assert methods["Invoice.Amount"]["source"] == "auto-inferred"
    assert methods["Receipt.Amount"]["source"] == "configured"


@pytest.mark.unit
def test_build_config_comparison():
    """Test configuration comparison"""
    configs = {
        "run-1": {"model": "claude-3", "temperature": 0.1},
        "run-2": {"model": "claude-4", "temperature": 0.2},
    }

    all_keys = set()
    for config in configs.values():
        all_keys.update(config.keys())

    config_diff = [
        {
            "setting": key,
            "values": {k: str(v.get(key, "N/A")) for k, v in configs.items()},
        }
        for key in all_keys
    ]

    assert len(config_diff) == 2
    assert "model" in [item["setting"] for item in config_diff]
    assert "temperature" in [item["setting"] for item in config_diff]


@pytest.mark.unit
def test_get_test_results_missing_metrics_returns_partial_not_raises():
    """When processing reached a terminal state but the evaluation aggregation
    never cached testRunResult (timed out / failed silently on a large run),
    get_test_results returns a structured partial TestRun instead of raising an
    opaque ValueError that leaves the UI spinning on "Loading..." (issue #358)."""
    test_run_id = "TEST-SET-ID"
    metadata = {
        "PK": f"testrun#{test_run_id}",
        "SK": "metadata",
        # Already terminal, so the status-refresh branch is skipped and we fall
        # straight through to the "no cached metrics" else branch.
        "Status": "COMPLETE",
        "TestSetId": "set-1",
        "TestSetName": "big-classification-set",
        "FilesCount": 3463,
        "CompletedFiles": 3460,
        "FailedFiles": 3,
        "CreatedAt": "2025-01-01T00:00:00Z",
        "Context": "ctx",
        "ConfigVersion": "v7",
        # No "testRunResult" key -> aggregation hasn't written metrics yet.
    }

    mock_table = Mock()
    mock_table.get_item.return_value = {"Item": metadata}

    with (
        patch.dict(os.environ, {"TRACKING_TABLE": "tracking"}),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
    ):
        result = index.get_test_results(test_run_id)

    assert result["testRunId"] == test_run_id
    # Reports the true terminal status rather than fabricating one.
    assert result["status"] == "COMPLETE"
    assert result["filesCount"] == 3463
    assert result["completedFiles"] == 3460
    assert result["failedFiles"] == 3
    assert result["testSetId"] == "set-1"
    assert result["configVersion"] == "v7"
    # Metric fields are absent (not yet computed) but must not be required.
    assert "overallAccuracy" not in result or result["overallAccuracy"] is None


def _stale_cache_metadata(test_run_id, cached_metrics, status="COMPLETE"):
    """Terminal test run whose testRunResult is present but may be stale."""
    return {
        "PK": f"testrun#{test_run_id}",
        "SK": "metadata",
        "Status": status,
        "TestSetId": "set-1",
        "TestSetName": "lending-test",
        "FilesCount": 10,
        "CompletedFiles": 10,
        "FailedFiles": 0,
        "CreatedAt": "2025-01-01T00:00:00Z",
        "testRunResult": cached_metrics,
    }


# A cache written before gradedPacketMetrics existed: every key the guard knew
# about at the time is present, so this is the exact shape of every historical
# test run's cache.
_PRE_GRADED_CACHE = {
    "overallAccuracy": 0.85,
    "weightedOverallScores": {"doc1.pdf": 0.9},
    "averageConfidence": 0.77,
    "accuracyBreakdown": {"precision": 0.9},
    "confusionMatrix": {"tp": 5},
    "fieldMetrics": {"Name": {"accuracy": 1.0}},
    "splitClassificationMetrics": {"page_level_accuracy": 0.9},
    "totalCost": 1.23,
    "costBreakdown": {},
}


@pytest.mark.unit
def test_stale_cache_serves_cached_metrics_and_queues_reaggregation():
    """A cache missing a key added by a later release must still resolve.

    The staleness check is a presence check, so every run cached before a new
    key landed trips it exactly once. If that path returned nothing,
    getTestRun would resolve to null — the UI renders "No test results found"
    and compareTestRuns silently drops the run — permanently, since nothing
    else re-enqueues a cache update for a run whose testRunResult exists.
    So: serve what we have, and recompute asynchronously.
    """
    test_run_id = "run-pre-graded"
    mock_table = Mock()
    mock_table.get_item.return_value = {
        "Item": _stale_cache_metadata(test_run_id, _PRE_GRADED_CACHE)
    }
    mock_sqs = Mock()

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
        patch.object(index, "_get_test_run_config", return_value={}),
    ):
        result = index.get_test_results(test_run_id)

    # The regression this pins: must not be None.
    assert result is not None
    assert result["testRunId"] == test_run_id
    # Metrics that WERE cached are still served, not discarded.
    assert result["overallAccuracy"] == 0.85
    assert result["splitClassificationMetrics"] == {"page_level_accuracy": 0.9}
    assert result["fieldMetrics"] == {"Name": {"accuracy": 1.0}}
    # The key the old cache lacks degrades to the "no data" shape the UI
    # already treats as "hide this panel".
    assert result["gradedPacketMetrics"] == {}
    # And a re-aggregation was queued so the next view has real values.
    mock_sqs.send_message.assert_called_once()
    queued_body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
    assert queued_body == {"testRunId": test_run_id}


@pytest.mark.unit
def test_fresh_cache_does_not_requeue_when_graded_metrics_legitimately_empty():
    """Convergence guard: no infinite re-aggregation loop.

    handle_cache_update_request always writes gradedPacketMetrics (defaulting
    to {}), so a run whose aggregation legitimately produces no graded metrics
    — single-section docs, or no gt/pred page overlap — must satisfy the
    presence check after one pass and never be re-queued again.
    """
    test_run_id = "run-post-graded-empty"
    # A "fresh" post-release cache: adds every key the guard now checks for
    # (gradedPacketMetrics + excludedDocumentCount + classificationErrors as of
    # this release). Add the new key here whenever one joins metrics_to_cache,
    # otherwise this test fails for the right reason — the guard would re-queue
    # a cache that is in fact complete.
    fresh_cache = dict(
        _PRE_GRADED_CACHE,
        gradedPacketMetrics={},
        excludedDocumentCount=0,
        classificationErrors={},
    )
    mock_table = Mock()
    mock_table.get_item.return_value = {
        "Item": _stale_cache_metadata(test_run_id, fresh_cache)
    }
    mock_sqs = Mock()

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
        patch.object(index, "_get_test_run_config", return_value={}),
    ):
        result = index.get_test_results(test_run_id)

    assert result is not None
    assert result["gradedPacketMetrics"] == {}
    mock_sqs.send_message.assert_not_called()


@pytest.mark.unit
def test_stale_cache_still_resolves_when_queueing_fails():
    """Re-aggregation is best-effort — a broken/unconfigured queue must not
    turn a readable (if stale) result into a failed query."""
    test_run_id = "run-no-queue"
    mock_table = Mock()
    mock_table.get_item.return_value = {
        "Item": _stale_cache_metadata(test_run_id, _PRE_GRADED_CACHE)
    }
    mock_sqs = Mock()
    mock_sqs.send_message.side_effect = Exception("queue unavailable")

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
        patch.object(index, "_get_test_run_config", return_value={}),
    ):
        result = index.get_test_results(test_run_id)

    assert result is not None
    assert result["overallAccuracy"] == 0.85


@pytest.mark.unit
def test_handler_field_routing():
    """Test GraphQL field routing"""

    def handler(event, context):
        field_name = event["info"]["fieldName"]

        if field_name == "getTestResults":
            return {"testRunId": event["arguments"]["testRunId"]}
        elif field_name == "getTestRuns":
            return [{"testRunId": "run-1"}]
        elif field_name == "compareTestRuns":
            return {"metrics": []}

        raise ValueError(f"Unknown field: {field_name}")

    # Test getTestResults
    event1 = {
        "info": {"fieldName": "getTestResults"},
        "arguments": {"testRunId": "test-123"},
    }
    result1 = handler(event1, {})
    assert result1["testRunId"] == "test-123"  # type: ignore[index]

    # Test getTestRuns
    event2 = {"info": {"fieldName": "getTestRuns"}, "arguments": {}}
    result2 = handler(event2, {})
    assert len(result2) == 1

    # Test unknown field
    event3 = {"info": {"fieldName": "unknownField"}, "arguments": {}}
    with pytest.raises(ValueError, match="Unknown field"):
        handler(event3, {})


# ---------------------------------------------------------------------------
# Issue #619: a test set name containing characters outside the SQL *identifier*
# allow-list (spaces, parentheses, quotes) made the Athena helpers raise, which
# threw away a perfectly good Stickler aggregation, cached nothing, and left the
# run permanently reporting EVALUATING with every file processed.
# ---------------------------------------------------------------------------

# The exact name that triggered the incident on the IDP1 stack.
_UNSAFE_RUN_ID = "ConfBench (light noise)-20260813-132501"


@pytest.mark.unit
def test_identifier_allow_list_still_rejects_injection():
    """The identifier guard must stay strict — it protects the database name."""
    for bad in ['db"; DROP TABLE x', "db; DROP TABLE x", "db name", "db'x", "db*", ""]:
        with pytest.raises(ValueError):
            index._validate_sql_input(bad, "database")

    # Hyphens and dots are legal — real database names need them.
    assert index._validate_sql_input("idp1-reporting-db", "database")


@pytest.mark.unit
def test_sql_literal_escapes_quotes():
    """Doubling `'` is the complete escape for a Trino single-quoted literal."""
    assert index._sql_literal("O'Brien", "test_run_id") == "O''Brien"
    # A classic literal-context break-out becomes inert data.
    assert index._sql_literal("x' OR '1'='1", "test_run_id") == "x'' OR ''1''=''1"
    # Backslash is NOT an escape character in Trino literals, so it must be left
    # alone here — doubling it would corrupt the matched value.
    assert index._sql_literal("a\\b", "test_run_id") == "a\\b"
    with pytest.raises(ValueError):
        index._sql_literal("", "test_run_id")


@pytest.mark.unit
def test_sql_like_prefix_neutralises_wildcards():
    """LIKE wildcards in a user-chosen name must match literally."""
    # `_` and `%` would otherwise widen the prefix match to other runs.
    assert index._sql_like_prefix("W2_Set", "test_run_id") == "W2\\_Set"
    assert index._sql_like_prefix("50%-set", "test_run_id") == "50\\%-set"
    # The escape character itself is escaped first, so it can't swallow the
    # character that follows it.
    assert index._sql_like_prefix("a\\_b", "test_run_id") == "a\\\\\\_b"
    # Spaces and parentheses — the issue #619 trigger — pass through untouched.
    assert index._sql_like_prefix(_UNSAFE_RUN_ID, "test_run_id") == _UNSAFE_RUN_ID


@pytest.mark.unit
def test_athena_evaluation_metrics_accepts_name_with_spaces_and_parens():
    """The regression: a parenthesised test set name must not raise.

    Before the fix `_validate_sql_input` was applied to test_run_id, which sits
    in a string-literal context, so any run whose test set name contained a
    space or paren failed aggregation outright.
    """
    captured = []

    def fake_execute(query, database):
        captured.append(query)
        return [{}]

    with (
        patch.dict(os.environ, {"ATHENA_DATABASE": "idp1-reporting-db"}),
        patch.object(index, "_execute_athena_query", side_effect=fake_execute),
    ):
        result = index._get_evaluation_metrics_from_athena(_UNSAFE_RUN_ID)

    assert result == {}  # empty Athena result set, but no exception
    assert captured, "query should have been built and executed"
    # The name is interpolated verbatim (nothing to escape) and paired with the
    # ESCAPE clause that _sql_like_prefix's output requires.
    assert f"LIKE '{_UNSAFE_RUN_ID}%'" in captured[0]
    assert "ESCAPE '\\'" in captured[0]


@pytest.mark.unit
def test_athena_cost_query_accepts_name_with_spaces_and_parens():
    """Same regression for the cost/metering query."""
    captured = []

    with (
        patch.dict(os.environ, {"ATHENA_DATABASE": "idp1-reporting-db"}),
        patch.object(
            index,
            "_execute_athena_query",
            side_effect=lambda q, d: captured.append(q) or [],
        ),
        patch.object(index, "_lookup_test_run_completed_at", return_value=None),
    ):
        result = index._get_cost_data_from_athena(_UNSAFE_RUN_ID)

    assert result == {"total_cost": 0, "cost_breakdown": {}}
    assert f"LIKE '{_UNSAFE_RUN_ID}/%'" in captured[0]
    # The embedded YYYYMMDD is still parsed out for partition pruning. With no
    # CompletedAt to size the window from, we fall back to the bounded 2-day
    # ``date IN (run_date, run_date+1)`` — see TestCostQueryDateWindow for the
    # derived-window cases.
    assert "date IN ('2026-08-13', '2026-08-14')" in captured[0]


@pytest.mark.unit
class TestCostQueryDateWindow:
    """``metering.date`` became COMPLETION time in the Phase-1 partitioning
    change, so a window fixed at ``run_date``/``run_date+1`` silently drops any
    run whose documents finish more than ~24h after the date embedded in its ID
    (HITL review, throttled or very large batches). The window is now derived
    from the run's own ``CompletedAt``.

    The opposite failure matters too: an unbounded upper edge scanned days of
    raw metering, hit ``HIVE_S3_THROTTLING`` and timed out the resolver's poll
    loop, leaving the UI's cost section empty. Hence the clamp.
    """

    RUN_ID = "lending-test-20260813-101500"

    def test_same_day_completion_keeps_the_two_day_window(self):
        """The overwhelmingly common case must not get more expensive: a run
        that completes the same day yields exactly the pre-change partitions."""
        assert (
            index._cost_query_date_filter(self.RUN_ID, "2026-08-13T11:02:00Z")
            == "AND date IN ('2026-08-13', '2026-08-14')"
        )

    def test_completion_just_before_midnight_still_covers_the_next_day(self):
        """A document completing at 23:58 has its metering row written moments
        later, possibly in the next date partition — that's the +1 day."""
        assert (
            index._cost_query_date_filter(self.RUN_ID, "2026-08-13T23:58:00Z")
            == "AND date IN ('2026-08-13', '2026-08-14')"
        )

    def test_multi_day_run_widens_the_window(self):
        """The regression this fixes: a 3-day HITL run's later completions used
        to fall outside the window and vanish from the reported cost."""
        assert index._cost_query_date_filter(self.RUN_ID, "2026-08-16T09:00:00Z") == (
            "AND date IN ('2026-08-13', '2026-08-14', '2026-08-15', "
            "'2026-08-16', '2026-08-17')"
        )

    def test_window_is_clamped_to_the_configured_maximum(self):
        """A pathological run (abandoned, or a clock problem putting
        CompletedAt months out) must not scan the whole lake."""
        sql = index._cost_query_date_filter(self.RUN_ID, "2026-09-12T09:00:00Z")
        assert sql.count("'") == 2 * (index._COST_QUERY_MAX_PARTITION_DAYS + 1)
        assert "'2026-08-13'" in sql  # run date is always the lower bound
        assert "'2026-09-12'" not in sql  # far edge dropped by the clamp

    def test_unparseable_completed_at_falls_back(self):
        assert (
            index._cost_query_date_filter(self.RUN_ID, "not-a-timestamp")
            == "AND date IN ('2026-08-13', '2026-08-14')"
        )

    def test_completed_at_before_run_date_falls_back_rather_than_inverting(self):
        """If the ID's date and the tracking row disagree, take the wider of the
        two — never emit an empty or inverted range."""
        assert (
            index._cost_query_date_filter(self.RUN_ID, "2026-08-01T09:00:00Z")
            == "AND date IN ('2026-08-13', '2026-08-14')"
        )

    def test_naive_completed_at_is_treated_as_utc(self):
        assert (
            index._cost_query_date_filter(self.RUN_ID, "2026-08-14T09:00:00")
            == "AND date IN ('2026-08-13', '2026-08-14', '2026-08-15')"
        )

    def test_run_id_without_a_date_leaves_the_query_unpruned(self):
        """Pre-existing behavior, unchanged: no parseable date means no filter
        (the selective ``document_id LIKE`` predicate still bounds the result)."""
        assert (
            index._cost_query_date_filter("no-date-here", "2026-08-14T09:00:00Z") == ""
        )

    def test_lookup_ignores_non_string_completed_at(self):
        """A stubbed DynamoDB client returns Mocks, not None. Only a real ISO
        string is usable; anything else must fall back quietly rather than reach
        the parser."""
        fake_table = Mock()
        fake_table.get_item.return_value = {"Item": {"CompletedAt": Mock()}}
        with patch.object(index.dynamodb, "Table", return_value=fake_table):
            with patch.dict(os.environ, {"TRACKING_TABLE": "t"}):
                assert index._lookup_test_run_completed_at(self.RUN_ID) is None

    def test_lookup_returns_the_stored_string(self):
        fake_table = Mock()
        fake_table.get_item.return_value = {
            "Item": {"CompletedAt": "2026-08-16T09:00:00Z"}
        }
        with patch.object(index.dynamodb, "Table", return_value=fake_table):
            with patch.dict(os.environ, {"TRACKING_TABLE": "t"}):
                assert (
                    index._lookup_test_run_completed_at(self.RUN_ID)
                    == "2026-08-16T09:00:00Z"
                )

    def test_lookup_failure_is_survivable(self):
        fake_table = Mock()
        fake_table.get_item.side_effect = RuntimeError("throttled")
        with patch.object(index.dynamodb, "Table", return_value=fake_table):
            with patch.dict(os.environ, {"TRACKING_TABLE": "t"}):
                assert index._lookup_test_run_completed_at(self.RUN_ID) is None


@pytest.mark.unit
def test_stickler_metrics_survive_athena_failure():
    """A failing Athena supplement must not discard good Stickler metrics.

    This is the core of issue #619: Stickler had already computed
    overall_accuracy=0.7232 for the run, but an exception from the *optional*
    Athena split-metrics call propagated out of _aggregate_test_run_metrics and
    into handle_cache_update_request's bare except, so nothing was ever cached.
    """
    stickler_body = {
        "overall_accuracy": 0.7232142857142857,
        "document_count": 10,
        "weighted_overall_scores": {"doc1.pdf": 0.6784},
        "average_confidence": 0.81,
        "confusion_matrix": {"tp": 5},
        "field_metrics": {"Name": {"accuracy": 1.0}},
        "graded_packet_metrics": {"mean": {"final_score": 0.7}},
    }
    mock_payload = Mock()
    mock_payload.read.return_value = json.dumps(
        {"statusCode": 200, "body": json.dumps(stickler_body)}
    )
    mock_lambda = Mock()
    mock_lambda.invoke.return_value = {"Payload": mock_payload}

    with (
        patch.dict(
            os.environ,
            {
                "TEST_EXECUTION_AGGREGATION_FUNCTION_ARN": "arn:aws:lambda:::function:agg"
            },
        ),
        patch.object(index, "lambda_client", mock_lambda),
        patch.object(index, "_get_test_run_config", return_value={}),
        patch.object(index, "_invoke_mlflow_logger"),
        # Both Athena supplements blow up, exactly as they did on the live stack.
        patch.object(
            index,
            "_get_evaluation_metrics_from_athena",
            side_effect=ValueError("test_run_id contains invalid characters"),
        ),
        patch.object(
            index,
            "_get_cost_data_from_athena",
            side_effect=ValueError("test_run_id contains invalid characters"),
        ),
    ):
        result = index._aggregate_test_run_metrics(_UNSAFE_RUN_ID)

    # The Stickler numbers survive...
    assert result["overall_accuracy"] == 0.7232142857142857
    assert result["document_count"] == 10
    assert result["field_metrics"] == {"Name": {"accuracy": 1.0}}
    assert result["average_confidence"] == 0.81
    # ...and the Athena-only extras degrade to their documented "no data" shape
    # rather than taking the whole aggregation down with them.
    assert result["split_classification_metrics"] == {}
    assert result["total_cost"] == 0
    assert result["cost_breakdown"] == {}


def _aggregation_lambda_returning(body):
    payload = Mock()
    payload.read.return_value = json.dumps(
        {"statusCode": 200, "body": json.dumps(body)}
    )
    lambda_client = Mock()
    lambda_client.invoke.return_value = {"Payload": payload}
    return lambda_client


@pytest.mark.unit
def test_a_classification_only_run_keeps_what_athena_cannot_supply():
    """A run with no extractable schema still caches what each document measured.

    Every section of a classification-only run is skipped for extraction, so the
    aggregation finds no comparisons and answers ``document_count`` 0 with the
    graded packet metrics, the classification errors and the excluded documents
    folded in. The Athena fallback that follows supplies none of those three,
    and for such a run only the split metrics and the cost, as mocked here: it
    averages confidence over attribute comparisons, and there are none.
    """
    test_run_id = "classify-only-run"
    graded = {
        "mean": {"final_score": 0.753, "v_measure": 0.756},
        "per_document": {"classify-only-run/p1.pdf": {"final_score": 0.753}},
        "document_count": 1,
    }
    errors = {
        "errors": [
            {
                "doc_key": "classify-only-run/p1.pdf",
                "section_id": "section_2",
                "kind": "split",
                "expected_class": "invoice",
                "predicted_class": "invoice",
                "expected_pages": [1, 2],
                "predicted_pages": [1],
            }
        ],
        "total": 1,
        "documents_affected": 1,
        "truncated": False,
    }
    aggregation = {
        "overall_accuracy": None,
        "weighted_overall_scores": {},
        "split_classification_metrics": {},
        "graded_packet_metrics": graded,
        "classification_errors": errors,
        "excluded_documents": ["classify-only-run/p1.pdf"],
        "excluded_document_count": 1,
        "document_count": 0,
    }
    athena_splits = {"total_pages": 3, "page_level_accuracy": 0.67}
    mock_table = Mock()

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_EXECUTION_AGGREGATION_FUNCTION_ARN": "arn:aws:lambda:::function:agg",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(
            index, "lambda_client", _aggregation_lambda_returning(aggregation)
        ),
        patch.object(index, "_get_test_run_config", return_value={}),
        patch.object(index, "_invoke_mlflow_logger"),
        patch.object(
            index,
            "_get_evaluation_metrics_from_athena",
            return_value={"split_classification_metrics": athena_splits},
        ),
        patch.object(
            index,
            "_get_cost_data_from_athena",
            return_value={"total_cost": 4.6, "cost_breakdown": {}},
        ),
    ):
        index.handle_cache_update_request(
            {"Records": [{"body": json.dumps({"testRunId": test_run_id})}]}, None
        )

    cached = mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"][
        ":metrics"
    ]
    assert cached["gradedPacketMetrics"] == index.float_to_decimal(graded)
    assert cached["classificationErrors"] == errors
    assert cached["excludedDocumentCount"] == 1
    assert cached["splitClassificationMetrics"] == index.float_to_decimal(athena_splits)
    assert cached["totalCost"] == index.float_to_decimal(4.6)


@pytest.mark.unit
def test_an_empty_aggregation_falls_back_without_inventing_fields():
    """An aggregation Lambda older than the carried fields adds none of them.

    The current Lambda answers with every one of them, as an empty value when
    nothing was measured, so only an older one omits them; the fallback result then
    keeps the shape it has with no aggregation answer at all.
    """
    with (
        patch.dict(
            os.environ,
            {
                "TEST_EXECUTION_AGGREGATION_FUNCTION_ARN": "arn:aws:lambda:::function:agg"
            },
        ),
        patch.object(
            index, "lambda_client", _aggregation_lambda_returning({"document_count": 0})
        ),
        patch.object(index, "_get_test_run_config", return_value={}),
        patch.object(index, "_invoke_mlflow_logger"),
        patch.object(index, "_get_evaluation_metrics_from_athena", return_value={}),
        patch.object(
            index,
            "_get_cost_data_from_athena",
            return_value={"total_cost": 0, "cost_breakdown": {}},
        ),
    ):
        result = index._aggregate_test_run_metrics("empty-run")

    assert set(result) == {
        "overall_accuracy",
        "weighted_overall_scores",
        "avg_weighted_overall_score",
        "average_confidence",
        "accuracy_breakdown",
        "split_classification_metrics",
        "total_cost",
        "cost_breakdown",
    }


def _load_aggregation_module():
    spec = importlib.util.spec_from_file_location(
        "aggregation_index",
        os.path.join(
            os.path.dirname(__file__),
            "../../../../patterns/unified/src/test_execution_aggregation_function/index.py",
        ),
    )
    if spec is None or spec.loader is None:
        raise ImportError("Could not load test_execution_aggregation_function module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_the_aggregation_lambdas_classification_only_answer_survives_the_fallback():
    """The carried fields are the ones the aggregation Lambda actually sends.

    Their keys are spelled out in that Lambda's file and again in the resolver's,
    so here the answer comes from the Lambda's own handler rather than being
    written by hand: a key renamed on either side fails this test instead of
    leaving the fallback with nothing to carry. The handler's document loader is
    mocked to return what a classification-only run's document holds: no
    extraction comparisons and no weighted score, so it is excluded from scoring,
    and a graded score and a classification mismatch from its own evaluation.
    """
    aggregation = _load_aggregation_module()
    test_run_id = "classify-only-run"
    doc_key = f"{test_run_id}/p1.pdf"
    graded_scores = {"final_score": 0.753, "v_measure": 0.756}
    mismatch = {
        "doc_key": doc_key,
        "section_id": "section_1",
        "kind": "class",
        "expected_class": "invoice",
        "predicted_class": "receipt",
        "expected_pages": [1],
        "predicted_pages": [1],
    }

    with (
        patch.dict(os.environ, {"TRACKING_TABLE": "tracking"}),
        patch.object(
            aggregation,
            "_load_comparison_results",
            return_value=(
                [],
                {},
                {doc_key: graded_scores},
                [doc_key],
                {doc_key: [mismatch]},
            ),
        ),
    ):
        response = aggregation.handler({"test_run_id": test_run_id}, None)

    assert response["statusCode"] == 200

    with (
        patch.dict(
            os.environ,
            {
                "TEST_EXECUTION_AGGREGATION_FUNCTION_ARN": "arn:aws:lambda:::function:agg"
            },
        ),
        patch.object(
            index,
            "lambda_client",
            _aggregation_lambda_returning(json.loads(response["body"])),
        ),
        patch.object(index, "_get_test_run_config", return_value={}),
        patch.object(index, "_invoke_mlflow_logger"),
        patch.object(index, "_get_evaluation_metrics_from_athena", return_value={}),
        patch.object(
            index,
            "_get_cost_data_from_athena",
            return_value={"total_cost": 0, "cost_breakdown": {}},
        ),
    ):
        result = index._aggregate_test_run_metrics(test_run_id)

    assert result["graded_packet_metrics"]["per_document"] == {doc_key: graded_scores}
    assert result["classification_errors"]["errors"] == [mismatch]
    assert result["excluded_document_count"] == 1


@pytest.mark.unit
def test_classification_errors_are_cached_and_served():
    """The aggregator's per-section class detail must survive the cache round-trip.

    Three hops have to agree for this to reach the UI — the aggregation Lambda's
    snake_case key, the camelCase key written to testRunResult, and the read path
    — and each is in a different file, so a rename in one is invisible until the
    panel is silently empty.
    """
    test_run_id = "run-with-class-errors"
    payload = {
        "errors": [
            {
                "doc_key": "d1.pdf",
                "section_id": "section_1",
                "kind": "class",
                "expected_class": "Invoice",
                "predicted_class": "Receipt",
                "expected_pages": [0],
                "predicted_pages": [0],
            }
        ],
        "total": 1,
        "documents_affected": 1,
        "truncated": False,
    }
    mock_table = Mock()
    mock_sqs = Mock()

    with (
        patch.dict(os.environ, {"TRACKING_TABLE": "tracking"}),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
        patch.object(
            index,
            "_aggregate_test_run_metrics",
            return_value={"classification_errors": payload},
        ),
    ):
        index.handle_cache_update_request(
            {"Records": [{"body": json.dumps({"testRunId": test_run_id})}]}, None
        )

    cached = mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"][
        ":metrics"
    ]
    assert cached["classificationErrors"] == payload

    # And the read path serves it rather than dropping it on the floor.
    mock_read_table = Mock()
    mock_read_table.get_item.return_value = {
        "Item": _stale_cache_metadata(
            test_run_id,
            dict(
                _PRE_GRADED_CACHE,
                gradedPacketMetrics={},
                excludedDocumentCount=0,
                classificationErrors=payload,
            ),
        )
    }
    with (
        patch.dict(os.environ, {"TRACKING_TABLE": "tracking"}),
        patch.object(index.dynamodb, "Table", return_value=mock_read_table),
        patch.object(index, "sqs", Mock()),
        patch.object(index, "_get_test_run_config", return_value={}),
    ):
        result = index.get_test_results(test_run_id)

    assert result["classificationErrors"] == payload


@pytest.mark.unit
def test_a_cache_written_before_this_release_serves_an_empty_panel():
    """An older cache must render as "nothing to show", not crash the query."""
    test_run_id = "run-pre-class-errors"
    mock_table = Mock()
    mock_table.get_item.return_value = {
        "Item": _stale_cache_metadata(test_run_id, dict(_PRE_GRADED_CACHE))
    }

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", Mock()),
        patch.object(index, "_get_test_run_config", return_value={}),
    ):
        result = index.get_test_results(test_run_id)

    assert result["classificationErrors"] == {}


@pytest.mark.unit
def test_missing_metrics_requeues_aggregation():
    """get_test_results must re-enqueue when a terminal run has no metrics.

    Nothing else will: the enqueue in get_test_run_status only fires on a status
    *transition*, and the stale-cache re-enqueue only fires when testRunResult
    already exists. Without this the run stays metric-less forever.
    """
    mock_table = Mock()
    mock_table.get_item.return_value = {
        "Item": {
            "PK": f"testrun#{_UNSAFE_RUN_ID}",
            "SK": "metadata",
            "Status": "COMPLETE",
            "FilesCount": 10,
            "CompletedFiles": 10,
            "FailedFiles": 0,
            # No testRunResult -> aggregation failed on its one attempt.
        }
    }
    mock_sqs = Mock()

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
        patch.object(index, "_claim_cache_update_slot", return_value=True),
    ):
        result = index.get_test_results(_UNSAFE_RUN_ID)

    assert result["status"] == "COMPLETE"
    mock_sqs.send_message.assert_called_once()
    body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
    assert body == {"testRunId": _UNSAFE_RUN_ID}


@pytest.mark.unit
def test_aborted_run_without_metrics_does_not_requeue():
    """An ABORTED run is not eligible for aggregation, matching the enqueue
    condition on the status-transition path."""
    mock_table = Mock()
    mock_table.get_item.return_value = {
        "Item": {
            "PK": "testrun#aborted-run",
            "SK": "metadata",
            "Status": "ABORTED",
            "FilesCount": 10,
            "CompletedFiles": 4,
            "FailedFiles": 0,
            "CompletedFilesCounted": True,
        }
    }
    mock_sqs = Mock()

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
    ):
        result = index.get_test_results("aborted-run")

    assert result["status"] == "ABORTED"
    mock_sqs.send_message.assert_not_called()


def _status_table_for(
    test_run_id, files, stored_status, with_metrics=False, queued_at=None
):
    """Mock tracking table where every file is fully processed and evaluated."""
    metadata = {
        "PK": f"testrun#{test_run_id}",
        "SK": "metadata",
        "Status": stored_status,
        "Files": files,
        "FilesCount": len(files),
        "CompletedAt": "2026-08-13T13:27:11.869523Z",
    }
    if with_metrics:
        metadata["testRunResult"] = {"overallAccuracy": 0.72}
    if queued_at is not None:
        metadata["CacheUpdateQueuedAt"] = queued_at

    def get_item(Key):
        if Key["PK"] == f"testrun#{test_run_id}":
            return {"Item": metadata}
        return {"Item": {"ObjectStatus": "COMPLETED", "EvaluationStatus": "COMPLETED"}}

    mock_table = Mock()
    mock_table.get_item.side_effect = get_item
    return mock_table


@pytest.mark.unit
def test_status_selfheals_when_already_terminal_without_metrics():
    """The observed symptom: EVALUATING badge with 10/10 processed, 0 evaluating.

    The run already reached COMPLETE on an earlier call, so the transition
    enqueue does not fire. This call must enqueue anyway, otherwise the badge
    never clears.
    """
    files = [f"doc{i}.pdf" for i in range(10)]
    mock_table = _status_table_for(_UNSAFE_RUN_ID, files, stored_status="COMPLETE")
    mock_sqs = Mock()

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
        patch.object(index, "_claim_cache_update_slot", return_value=True),
    ):
        result = index.get_test_run_status(_UNSAFE_RUN_ID)

    # The reported state that looked self-contradictory to the user.
    assert result["status"] == "EVALUATING"
    assert result["completedFiles"] == 10
    assert result["evaluatingFiles"] == 0
    # ...now accompanied by a recovery attempt.
    mock_sqs.send_message.assert_called_once()


@pytest.mark.unit
def test_status_does_not_requeue_once_metrics_are_cached():
    """Convergence: once testRunResult exists, COMPLETE is reported and no
    further aggregation is enqueued."""
    files = [f"doc{i}.pdf" for i in range(10)]
    mock_table = _status_table_for(
        "good-run", files, stored_status="COMPLETE", with_metrics=True
    )
    mock_sqs = Mock()

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
    ):
        result = index.get_test_run_status("good-run")

    assert result["status"] == "COMPLETE"
    mock_sqs.send_message.assert_not_called()


@pytest.mark.unit
def test_cache_update_throttle_collapses_concurrent_enqueues():
    """Three concurrent readers raced during the incident, each firing its own
    redundant aggregation. The conditional-write claim collapses them to one."""
    mock_sqs = Mock()

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index, "sqs", mock_sqs),
        # Loser of the race: the condition expression failed.
        patch.object(index, "_claim_cache_update_slot", return_value=False),
    ):
        assert index._queue_cache_update("run-1") is False

    mock_sqs.send_message.assert_not_called()

    # throttle_seconds=0 bypasses the claim entirely (used for forced backfills).
    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index, "sqs", mock_sqs),
        patch.object(index, "_claim_cache_update_slot", return_value=False),
    ):
        assert index._queue_cache_update("run-1", throttle_seconds=0) is True

    mock_sqs.send_message.assert_called_once()


@pytest.mark.unit
def test_claim_cache_update_slot_fails_open():
    """A broken throttle must never block a legitimate recompute."""
    mock_table = Mock()
    mock_table.update_item.side_effect = Exception("dynamo unavailable")

    with (
        patch.dict(os.environ, {"TRACKING_TABLE": "tracking"}),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
    ):
        assert index._claim_cache_update_slot("run-1", 300) is True


@pytest.mark.unit
def test_claim_cache_update_slot_denies_on_condition_failure():
    """A ConditionalCheckFailedException means someone else already claimed it."""
    from botocore.exceptions import ClientError

    mock_table = Mock()
    mock_table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
    )

    with (
        patch.dict(os.environ, {"TRACKING_TABLE": "tracking"}),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
    ):
        assert index._claim_cache_update_slot("run-1", 300) is False


# ---------------------------------------------------------------------------
# Follow-up to #619 review: the "terminal but no metrics -> EVALUATING" rule is
# now defined once, and the throttle window is read from the item already in
# hand rather than by attempting a conditional write on every 5-second poll.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "status,has_metrics,expected",
    [
        # Terminal and metrics missing -> the aggregation really is outstanding.
        ("COMPLETE", False, "EVALUATING"),
        ("PARTIAL_COMPLETE", False, "EVALUATING"),
        # Metrics present -> report the true status.
        ("COMPLETE", True, "COMPLETE"),
        ("PARTIAL_COMPLETE", True, "PARTIAL_COMPLETE"),
        # ABORTED is terminal but is NOT eligible for aggregation, so it must
        # never be masked as EVALUATING however its metrics look.
        ("ABORTED", False, "ABORTED"),
        ("ABORTED", True, "ABORTED"),
        # Non-terminal statuses pass straight through.
        ("RUNNING", False, "RUNNING"),
        ("QUEUED", False, "QUEUED"),
    ],
)
def test_display_status_rule(status, has_metrics, expected):
    """One truth table for the badge, shared by all three resolvers."""
    item = {"testRunResult": {"overallAccuracy": 0.7}} if has_metrics else {}
    assert index._display_status(item, status) == expected
    assert index._awaiting_metrics(item, status) is (expected == "EVALUATING")


@pytest.mark.unit
def test_build_test_run_list_uses_shared_rule_but_does_not_enqueue():
    """The list view shows EVALUATING but must not fan out an enqueue per row.

    One list render covers an arbitrary number of runs; enqueueing for each stuck
    row would turn a page load into a burst of multi-minute aggregations. The
    per-row getTestRunStatus poll drives recovery instead.
    """
    items = [
        {
            "TestRunId": "stuck-1",
            "Status": "COMPLETE",
            "CreatedAt": "2026-08-13T13:25:01.571600Z",
        },
        {
            "TestRunId": "stuck-2",
            "Status": "PARTIAL_COMPLETE",
            "CreatedAt": "2026-08-13T13:25:01.571600Z",
        },
        {
            "TestRunId": "aborted-1",
            "Status": "ABORTED",
            "CreatedAt": "2026-08-13T13:25:01.571600Z",
        },
        {
            "TestRunId": "good-1",
            "Status": "COMPLETE",
            "testRunResult": {"overallAccuracy": 0.72},
            "CreatedAt": "2026-08-13T13:25:01.571600Z",
        },
    ]
    mock_sqs = Mock()

    with patch.object(index, "sqs", mock_sqs):
        result = index._build_test_run_list(items)

    by_id = {r["testRunId"]: r["status"] for r in result}
    assert by_id["stuck-1"] == "EVALUATING"
    assert by_id["stuck-2"] == "EVALUATING"
    assert by_id["aborted-1"] == "ABORTED"
    assert by_id["good-1"] == "COMPLETE"
    # The regression this pins: rendering a list is not a write path.
    mock_sqs.send_message.assert_not_called()


@pytest.mark.unit
def test_cache_update_recently_queued_window():
    """Recent -> suppress; stale/absent/garbage -> fall through to the claim."""
    now = datetime.now(timezone.utc)

    # Inside the window.
    assert index._cache_update_recently_queued(
        {"CacheUpdateQueuedAt": (now - timedelta(seconds=30)).isoformat()}, 300
    )
    # Outside the window -> due for another attempt.
    assert not index._cache_update_recently_queued(
        {"CacheUpdateQueuedAt": (now - timedelta(seconds=301)).isoformat()}, 300
    )
    # Never queued.
    assert not index._cache_update_recently_queued({}, 300)
    # Throttling explicitly disabled.
    assert not index._cache_update_recently_queued(
        {"CacheUpdateQueuedAt": now.isoformat()}, 0
    )
    # Unreadable value must not silently suppress recovery.
    assert not index._cache_update_recently_queued(
        {"CacheUpdateQueuedAt": "not-a-timestamp"}, 300
    )
    assert not index._cache_update_recently_queued({"CacheUpdateQueuedAt": 12345}, 300)
    # A naive timestamp is treated as UTC rather than raising on the subtraction.
    assert index._cache_update_recently_queued(
        {"CacheUpdateQueuedAt": now.replace(tzinfo=None).isoformat()}, 300
    )
    # A future timestamp (clock skew) is not treated as "recent" in a way that
    # could suppress recovery forever -- negative age falls through.
    assert not index._cache_update_recently_queued(
        {"CacheUpdateQueuedAt": (now + timedelta(seconds=60)).isoformat()}, 300
    )


@pytest.mark.unit
def test_status_poll_skips_conditional_write_when_recently_queued():
    """The hot path: a 5-second poll on a stuck run must not write to DynamoDB.

    A rejected conditional write still consumes write capacity, so with the
    aggregation already in flight the poll should not attempt one at all.
    """
    files = [f"doc{i}.pdf" for i in range(10)]
    recent = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    mock_table = _status_table_for(
        "in-flight-run", files, stored_status="COMPLETE", queued_at=recent
    )
    mock_sqs = Mock()
    mock_claim = Mock(return_value=True)

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
        patch.object(index, "_claim_cache_update_slot", mock_claim),
    ):
        result = index.get_test_run_status("in-flight-run")

    # Still reports the outstanding aggregation to the user...
    assert result["status"] == "EVALUATING"
    assert result["completedFiles"] == 10
    # ...without a duplicate enqueue or the conditional write behind it.
    mock_sqs.send_message.assert_not_called()
    mock_claim.assert_not_called()
    mock_table.update_item.assert_not_called()


@pytest.mark.unit
def test_status_poll_retries_once_throttle_window_expires():
    """Convergence: after the window lapses, recovery is attempted again."""
    files = [f"doc{i}.pdf" for i in range(10)]
    stale = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
    mock_table = _status_table_for(
        "stale-claim-run", files, stored_status="COMPLETE", queued_at=stale
    )
    mock_sqs = Mock()

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", mock_sqs),
        patch.object(index, "_claim_cache_update_slot", return_value=True),
    ):
        result = index.get_test_run_status("stale-claim-run")

    assert result["status"] == "EVALUATING"
    mock_sqs.send_message.assert_called_once()


@pytest.mark.unit
def test_average_weighted_overall_score():
    """The resolver's local roll-up of the per-document weighted scores.

    Mirrors ``average_weighted_overall_score`` in the test execution aggregation
    Lambda; the two are separate copies because this Lambda ships without
    ``idp_common``, so the behaviour has to be pinned on both sides.
    """
    assert index._average_weighted_overall_score(
        {"doc1.pdf": 0.9, "doc2.pdf": 0.7}
    ) == pytest.approx(0.8)

    # DynamoDB hands back Decimal, which must average to a plain float.
    averaged = index._average_weighted_overall_score(
        {"doc1.pdf": Decimal("0.9"), "doc2.pdf": Decimal("0.7")}
    )
    assert averaged == pytest.approx(0.8)
    assert isinstance(averaged, float)

    # None and non-finite values are excluded from numerator AND denominator, so
    # one unscored document can't drag the run-level figure down and one NaN
    # can't poison the whole mean. Matches the UI's parseWeightedOverallScoresFinite.
    assert index._average_weighted_overall_score(
        {"doc1.pdf": 0.9, "doc2.pdf": 0.7, "doc3.pdf": None}
    ) == pytest.approx(0.8)
    assert index._average_weighted_overall_score(
        {"doc1.pdf": 0.9, "doc2.pdf": 0.7, "doc3.pdf": float("nan")}
    ) == pytest.approx(0.8)
    assert index._average_weighted_overall_score(
        {"doc1.pdf": 0.9, "doc2.pdf": 0.7, "doc3.pdf": float("inf")}
    ) == pytest.approx(0.8)

    # No usable scores -> None, never 0.0 (which would read as "perfectly bad").
    assert index._average_weighted_overall_score({}) is None
    assert index._average_weighted_overall_score(None) is None
    assert index._average_weighted_overall_score({"doc1.pdf": None}) is None


@pytest.mark.unit
def test_resolve_avg_weighted_overall_score_keeps_a_legitimate_zero():
    """A supplied 0.0 is a real score and must survive the fallback.

    The regression this pins: a truthiness check would treat 0.0 as "absent" and
    recompute, which returns None when the per-document map isn't in the cache —
    silently turning a run that scored 0 into a run with no score at all.
    """
    assert index._resolve_avg_weighted_overall_score(0.0, None) == 0.0
    assert index._resolve_avg_weighted_overall_score(Decimal("0"), {}) == 0.0

    # A supplied value always wins over recomputation, even when both exist.
    assert index._resolve_avg_weighted_overall_score(
        0.5, {"doc1.pdf": 0.9}
    ) == pytest.approx(0.5)

    # Absent -> recompute from the per-document map.
    assert index._resolve_avg_weighted_overall_score(
        None, {"doc1.pdf": 0.9, "doc2.pdf": 0.7}
    ) == pytest.approx(0.8)

    # Absent with nothing to recompute from -> None.
    assert index._resolve_avg_weighted_overall_score(None, None) is None


@pytest.mark.unit
def test_pre_existing_cache_gets_avg_weighted_score_recomputed():
    """Runs cached before ``avgWeightedOverallScore`` existed still report it.

    ``_PRE_GRADED_CACHE`` is the real shape of a historical cache entry: it has
    ``weightedOverallScores`` but no run-level average. Without the recompute
    fallback the field would resolve to null for every run that predates it.
    """
    test_run_id = "run-pre-avg-weighted"
    cache = dict(_PRE_GRADED_CACHE)
    cache["weightedOverallScores"] = {"doc1.pdf": Decimal("0.9"), "doc2.pdf": None}
    assert "avgWeightedOverallScore" not in cache

    mock_table = Mock()
    mock_table.get_item.return_value = {
        "Item": _stale_cache_metadata(test_run_id, cache)
    }

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", Mock()),
        patch.object(index, "_get_test_run_config", return_value={}),
    ):
        result = index.get_test_results(test_run_id)

    assert result["avgWeightedOverallScore"] == pytest.approx(0.9)


@pytest.mark.unit
class TestDraftLabelingRunsAreNotAwaitingMetrics:
    """A draft-labeling run CREATES the baseline, so it can never have metrics.

    Treating one as "awaiting" cost twice: it badged EVALUATING indefinitely
    (observed live — a run COMPLETE with 100/100 documents processed and
    CompletedAt set, still EVALUATING three days later), and every view enqueued a
    full aggregation, re-reading every document's results.json from S3, to compute
    extraction metrics that are structurally empty because there is nothing to
    score against.
    """

    def test_a_draft_labeling_run_is_never_awaiting_metrics(self):
        item = {
            "Status": "COMPLETE",
            "Purpose": "draft-labeling",
            "FilesCount": 100,
            "CompletedFiles": 100,
        }

        assert index._awaiting_metrics(item, "COMPLETE") is False
        # And therefore does not render the EVALUATING badge.
        assert index._display_status(item, "COMPLETE") == "COMPLETE"

    def test_a_scoring_run_with_no_metrics_still_awaits(self):
        """The behaviour this must not break: a real scored run genuinely is
        pending its aggregation, and the badge is how that is communicated."""
        item = {"Status": "COMPLETE", "Purpose": "scoring"}

        assert index._awaiting_metrics(item, "COMPLETE") is True
        assert index._display_status(item, "COMPLETE") == "EVALUATING"

    def test_a_scoring_run_with_metrics_does_not_await(self):
        item = {"Status": "COMPLETE", "Purpose": "scoring", "testRunResult": {"x": 1}}

        assert index._awaiting_metrics(item, "COMPLETE") is False

    def test_a_run_predating_Purpose_falls_back_to_its_context(self):
        """Records created before Purpose was persisted carry only the free-text
        Context. Matched exactly, not as a substring: a user-typed context that
        merely mentions labeling must not silently suppress a real run's badge."""
        legacy = {"Status": "COMPLETE", "Context": "Draft labeling run"}
        assert index._awaiting_metrics(legacy, "COMPLETE") is False

        lookalike = {"Status": "COMPLETE", "Context": "Draft labeling run for Q3"}
        assert index._awaiting_metrics(lookalike, "COMPLETE") is True

    def test_purpose_wins_over_a_misleading_context(self):
        """A persisted Purpose is authoritative; Context is user-supplied text."""
        item = {
            "Status": "COMPLETE",
            "Purpose": "scoring",
            "Context": "Draft labeling run",
        }

        assert index._awaiting_metrics(item, "COMPLETE") is True

    def test_the_purpose_is_reported_to_the_ui_not_just_used_internally(self):
        """The rule has one home, and the UI has to be able to reach the verdict.

        Without this the UI can only guess from the free-text Context, which is the
        very thing the exact-match fallback exists to distrust — and it warned that
        accuracy metrics "are not available" on a run that can never have them,
        describing the expected outcome as a fault.
        """
        draft = {"Status": "COMPLETE", "Purpose": "draft-labeling"}
        scoring = {"Status": "COMPLETE", "Purpose": "scoring"}

        assert index._is_draft_labeling_run(draft) is True
        assert index._is_draft_labeling_run(scoring) is False

        # The same distrust of Context that _awaiting_metrics applies.
        assert index._is_draft_labeling_run({"Context": "Draft labeling run"}) is True
        assert (
            index._is_draft_labeling_run({"Context": "Draft labeling run for Q3"})
            is False
        )


# --------------------------------------------------------------------------- #
# Evaluation that never runs, and the run status that waited for it anyway
# (#1330)
#
# A document's EvaluationStatus is what tells this resolver whether a document
# is finished. Its default for a value it does not recognise is "still
# evaluating", and a run holding one such document cannot reach a terminal
# status — so a status the pipeline writes but this file does not name, or one
# the pipeline never writes at all, pins the run at EVALUATING with every
# document showing Completed and no timeout anywhere to break the tie. Both
# shapes existed: DISABLED was not written, and TIMED_OUT was unnamed.
# --------------------------------------------------------------------------- #

_EVALUATION_FUNCTION = os.path.join(
    os.path.dirname(__file__),
    "../../../../patterns/unified/src/evaluation_function/index.py",
)


# The attribute's other two writers. Promoting a document to an evaluation
# baseline overwrites EvaluationStatus with one of these, so a test-run document
# promoted from the document list arrives in the run-status loop carrying one —
# a value the pipeline's enum does not contain and which the loop therefore has
# to classify anyway. Read from source for the same reason as the enum.
#
# ⚠️ What is NOT checked: that these are still the only writers. The three are
# the writers as of #1330, established by grepping the tree for somewhere a
# status *originates* rather than is copied along —
#
#     git ls-files '*.py' | xargs grep -n 'evaluation_status *=[^=]'
#
# which today finds these three plus six sites that propagate an existing value
# as a keyword argument. A fourth writer added later fails nothing here, and no
# cheap derivation separates a status literal from the environment-variable
# names that share the prefix (EVALUATION_BASELINE_BUCKET and friends appear in
# twenty tracked files). So this is a declared residual, not a closure: the
# closure below is over the values these three writers can set, and the two
# sanity assertions in the test are what stop a writer going quietly inert.
_BASELINE_STATUS_WRITERS = (
    os.path.join(
        os.path.dirname(__file__),
        *([os.pardir] * 4),
        "nested",
        "api-resolvers",
        "src",
        "lambda",
        "copy_to_baseline_resolver",
        "index.py",
    ),
    os.path.join(
        os.path.dirname(__file__),
        *([os.pardir] * 4),
        "lib",
        "idp_sdk",
        "idp_sdk",
        "_core",
        "evaluation_processor.py",
    ),
)


def _pipeline_evaluation_statuses():
    """Every ``EvaluationStatus`` value the pipeline can write, read from source.

    Parsed rather than imported, because all this needs is the enum's members
    and parsing cannot run the module's import-time code at all. Naming the path
    as one slash-joined literal is deliberate: that spelling is what enrols the
    module in ``test_handler_imports_are_region_free``'s census, and it belongs
    there — it imports cleanly with no AWS environment at all.
    """
    import ast

    tree = ast.parse(open(_EVALUATION_FUNCTION).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "EvaluationStatus":
            return {
                stmt.value.value
                for stmt in node.body
                if isinstance(stmt, ast.Assign)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            }
    raise AssertionError("EvaluationStatus enum not found in the evaluation function")


def _baseline_copy_statuses():
    """The ``BASELINE_*`` values the two copy-to-baseline writers can set.

    Collected by scanning their source for the literals rather than listing them
    here, so a fourth outcome added to either writer widens the universe the
    closure test below is checked against instead of silently falling outside it.

    The lookarounds matter: without them the pattern also matches the tail of
    ``EVALUATION_BASELINE_BUCKET``, and an environment-variable name would be
    demanded of the run-status classification as though it were a status.
    """
    found = set()
    for path in _BASELINE_STATUS_WRITERS:
        found |= set(
            re.findall(r"(?<![A-Z_])BASELINE_[A-Z_]+(?![A-Z_])", open(path).read())
        )
    return found


@pytest.mark.unit
def test_every_evaluation_status_any_writer_can_set_is_classified():
    """The class-level guard, and the reason this is a test rather than a comment.

    Both halves of #1330 were a status the run-status loop had no branch for.
    Counting an unknown one as evaluating is the right default for a *reader*
    (a status it cannot interpret may well mean work in progress) and a terrible
    one as a steady state, because nothing ever revisits the decision. So the
    rule enforced here is a closure: every value ANY writer of the attribute can
    set is classified as settled, unsuccessful or in-flight, and in exactly one
    of the three. Adding a status to any of those writers without coming here
    fails this test.

    The universe is every known writer, not the pipeline's enum alone, because
    the enum is only one of three and the other two were the easiest thing to
    miss:
    they describe a different activity (promoting a document to a baseline) and
    write over whatever evaluation left in the attribute. A closure asserted
    over one writer would have read as cover for all of them.
    """
    statuses = _pipeline_evaluation_statuses() | _baseline_copy_statuses()
    # Sanity-check both parses: an empty or tiny set would make the closure
    # assertion below vacuously true.
    assert {"COMPLETED", "FAILED", "RUNNING", "DISABLED", "TIMED_OUT"} <= statuses
    assert {
        "BASELINE_COPYING",
        "BASELINE_AVAILABLE",
        "BASELINE_ERROR",
    } <= statuses

    partitions = {
        "settled": index._EVAL_STATUS_SETTLED,
        "unsuccessful": index._EVAL_STATUS_UNSUCCESSFUL,
        "in flight": index._EVAL_STATUS_IN_FLIGHT,
    }
    for status in statuses:
        holders = [name for name, values in partitions.items() if status in values]
        assert holders, (
            f"EvaluationStatus {status} is classified nowhere in the run-status "
            "loop, so every test run containing such a document will report "
            "EVALUATING indefinitely"
        )
        assert len(holders) == 1, (
            f"EvaluationStatus {status} is in more than one partition: {holders}"
        )

    # And nothing is classified that no writer can produce — a stale entry here
    # is a branch no document can reach, which reads as cover it is not.
    classified = set().union(*partitions.values())
    assert classified <= statuses, (
        f"classified but unwritable: {sorted(classified - statuses)}"
    )


def _run_table(test_run_id, files, eval_status, stored_status="RUNNING", metadata=None):
    """Mock tracking table: every file processed, each with ``eval_status``.

    ``eval_status`` may be a single value applied to every file, or a list of one
    value per file. ``None`` means the attribute is absent, which is the shape a
    document processed with evaluation switched off had before #1330.
    """
    per_file = (
        eval_status if isinstance(eval_status, list) else [eval_status] * len(files)
    )
    assert len(per_file) == len(files)
    by_key = dict(zip(files, per_file))

    item = {
        "PK": f"testrun#{test_run_id}",
        "SK": "metadata",
        "Status": stored_status,
        "Files": files,
        "FilesCount": len(files),
    }
    item.update(metadata or {})

    def get_item(Key):
        if Key["PK"] == f"testrun#{test_run_id}":
            return {"Item": item}
        file_key = Key["PK"].split("/", 1)[1]
        doc = {"ObjectStatus": "COMPLETED"}
        if by_key[file_key] is not None:
            doc["EvaluationStatus"] = by_key[file_key]
        return {"Item": doc}

    def update_item(Key, UpdateExpression=None, ExpressionAttributeValues=None, **kw):
        # Applied for real, so a reader called after the status transition sees
        # what the transition stored. The Executions list reads the stored
        # Status, which is the asymmetry the cross-reader test below is about.
        if ":status" in (ExpressionAttributeValues or {}):
            item["Status"] = ExpressionAttributeValues[":status"]
        return {}

    mock_table = Mock()
    mock_table.get_item.side_effect = get_item
    mock_table.update_item.side_effect = update_item
    # The run record itself, for a reader that takes the item rather than an id.
    mock_table.metadata = item
    return mock_table


def _status_of(test_run_id, mock_table, sqs=None):
    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", sqs or Mock()),
        patch.object(index, "_claim_cache_update_slot", return_value=True),
    ):
        return index.get_test_run_status(test_run_id)


@pytest.mark.unit
def test_a_run_whose_documents_skipped_evaluation_reaches_a_terminal_status():
    """The reported bug: evaluation.enabled false, every document Completed, run
    stuck on EVALUATING.

    The run now settles as COMPLETE and an aggregation is enqueued, which is
    what clears the badge one pass later (and is where the run's cost comes
    from). Both halves matter: a terminal status with nothing enqueued is how
    the first version of this fix left a run with no ground truth stuck in a
    different way — see the cross-reader test below.
    """
    files = [f"doc{i}.pdf" for i in range(3)]
    table = _run_table("off-run", files, "DISABLED")
    sqs = Mock()
    result = _status_of("off-run", table, sqs=sqs)

    assert table.metadata["Status"] == "COMPLETE"
    assert result["completedFiles"] == 3
    assert result["evaluatingFiles"] == 0
    assert result["progress"] == 100
    sqs.send_message.assert_called_once()


@pytest.mark.unit
def test_a_run_that_skipped_evaluation_reports_complete_once_aggregated():
    """And converges, rather than reporting EVALUATING for good.

    One pass of the cache update is enough however little the aggregation found,
    which is the convergence the badge rule relies on.
    """
    files = [f"doc{i}.pdf" for i in range(3)]
    table = _run_table(
        "off-run-cached",
        files,
        "DISABLED",
        stored_status="COMPLETE",
        metadata={"testRunResult": {"overallAccuracy": None, "totalCost": 1.25}},
    )
    sqs = Mock()
    result = _status_of("off-run-cached", table, sqs=sqs)

    assert result["status"] == "COMPLETE"
    assert result["completedFiles"] == 3
    sqs.send_message.assert_not_called()


@pytest.mark.unit
def test_a_timed_out_evaluation_is_a_failure_not_an_eternal_wait():
    """TIMED_OUT is terminal. It was in no branch, so it fell to the unknown-status
    default and held the whole run short of a terminal status — the same symptom
    as #1330 from a different direction, and reachable since #917 made the
    timeout path stamp it."""
    files = ["a.pdf", "b.pdf", "c.pdf"]
    mock_table = _run_table(
        "timeout-run",
        files,
        ["COMPLETED", "TIMED_OUT", "COMPLETED"],
        # Two documents were scored, so metrics do exist for this run; cache them
        # so the assertion is about the counting rather than about the separate
        # "terminal but not yet aggregated" badge rule.
        metadata={"testRunResult": {"overallAccuracy": 0.9}},
    )
    result = _status_of("timeout-run", mock_table)

    assert result["status"] == "PARTIAL_COMPLETE"
    assert result["failedFiles"] == 1
    assert result["evaluatingFiles"] == 0


@pytest.mark.unit
def test_an_unrecorded_status_completes_when_the_run_disabled_evaluation():
    """The rescue for a run that predates the DISABLED stamp.

    Such a run's documents carry no EvaluationStatus at all and nothing will ever
    write one, so the only remaining evidence is the configuration the run
    captured. Without this, deploying the fix leaves every already-stuck run
    stuck.
    """
    files = ["a.pdf", "b.pdf"]
    table = _run_table(
        "legacy-off-run",
        files,
        None,
        metadata={"Config": {"evaluation": {"enabled": False}}},
    )
    result = _status_of("legacy-off-run", table)

    assert table.metadata["Status"] == "COMPLETE"
    assert result["completedFiles"] == 2
    assert result["evaluatingFiles"] == 0


@pytest.mark.unit
def test_an_unrecorded_status_still_means_evaluating_when_evaluation_is_on():
    """The case the old branch was written for, which must keep working.

    A document whose evaluation genuinely has not started yet looks exactly like
    one that will never be evaluated. Reading the configuration is what separates
    them; reading neither, and completing the run regardless, would report
    results before they exist.
    """
    files = ["a.pdf", "b.pdf"]
    mock_table = _run_table(
        "on-run", files, None, metadata={"Config": {"evaluation": {"enabled": True}}}
    )
    result = _status_of("on-run", mock_table)

    assert result["status"] == "EVALUATING"
    assert result["evaluatingFiles"] == 2
    assert result["completedFiles"] == 0


@pytest.mark.unit
def test_evaluation_status_casing_does_not_decide_whether_a_run_can_finish():
    """Writers have been inconsistent about casing, which the other two document
    probes in this file already normalize for. Here it is not cosmetic: an
    unexpected spelling lands in the unknown-status branch and the run never
    finishes."""
    files = ["a.pdf", "b.pdf"]
    mock_table = _run_table(
        "case-run",
        files,
        "completed",
        metadata={"testRunResult": {"overallAccuracy": 0.9}},
    )
    result = _status_of("case-run", mock_table)

    assert result["status"] == "COMPLETE"
    assert result["completedFiles"] == 2


@pytest.mark.unit
def test_a_blank_status_is_read_as_unrecorded_rather_than_unrecognised():
    """An empty string means nothing was written, which is the unrecorded case
    (and so answerable from the configuration), not an unknown status."""
    files = ["a.pdf"]
    table = _run_table(
        "blank-run",
        files,
        "   ",
        metadata={"Config": {"evaluation": {"enabled": False}}},
    )
    _status_of("blank-run", table)

    assert table.metadata["Status"] == "COMPLETE"


@pytest.mark.unit
def test_an_unrecognised_status_keeps_the_conservative_default():
    """Not every unknown value can be assumed finished. A status this file does
    not know may well mean work in progress, so the default stays "evaluating" —
    the closure test above is what stops that default becoming a run's permanent
    state."""
    files = ["a.pdf"]
    result = _status_of("odd-run", _run_table("odd-run", files, "REJUVENATING"))

    assert result["status"] == "EVALUATING"
    assert result["evaluatingFiles"] == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "stored",
    [
        False,
        True,
        "false",
        "False",
        "FALSE",
        "true",
        "no",
        "off",
        "0",
        "1",
        0,
        1,
        # Decimal is the type both storage shapes actually produce for a number:
        # the compressed path parses with parse_float=Decimal and DynamoDB hands
        # back Decimal on the legacy-inline path. A sweep of ints alone would not
        # have covered either.
        Decimal(0),
        Decimal("0.0"),
        Decimal(1),
        0.0,
        1.0,
    ],
)
def test_the_disabled_reading_agrees_with_the_pipelines(stored):
    """Both ends must read ``evaluation.enabled`` the same way.

    The pipeline reads it through pydantic (``EvaluationConfig.enabled: bool``),
    which accepts several string spellings as well as real booleans — a stored
    ``"false"`` genuinely stops evaluation running. This resolver decides, from
    the same value, whether to stop waiting for results. If the two disagree, one
    of them is wrong about whether any document was scored.

    Compared against the real model rather than a hand-written table of
    expectations, because a table is exactly what would drift from pydantic's
    coercion rules and the divergence would be invisible in review.
    """
    from idp_common.config.models import EvaluationConfig

    pipeline_disabled = not EvaluationConfig(enabled=stored).enabled
    item = {"Config": {"evaluation": {"enabled": stored}}}

    assert index._ran_with_evaluation_disabled(item) is pipeline_disabled


@pytest.mark.unit
@pytest.mark.parametrize(
    "config",
    [
        {},
        {"evaluation": {}},
        {"evaluation": {"model": "nova-pro"}},
        {"evaluation": "enabled"},
        # A value pydantic itself would reject. Evaluation cannot be reported as
        # skipped on the strength of a configuration the pipeline could not even
        # load — a wrong "disabled" verdict reports a run complete while results
        # are still arriving.
        {"evaluation": {"enabled": "maybe"}},
    ],
)
def test_only_a_configuration_that_says_so_counts_as_disabled(config):
    """``enabled`` defaults to true, so silence is not consent here."""
    assert index._ran_with_evaluation_disabled({"Config": config}) is False


@pytest.mark.unit
def test_the_disabled_reading_works_on_the_shape_runs_are_actually_stored_in():
    """Runs store their configuration gzipped, not inline.

    The inline form the other tests use is the legacy one. A check that only
    worked on it would pass everywhere and answer False for every run created
    since compression landed — i.e. for every run that can hit #1330.
    """
    body = json.dumps({"evaluation": {"enabled": False}}).encode("utf-8")
    item = {
        "_config_storage": "compressed",
        "_compressed_config": gzip.compress(body),
    }

    assert index._ran_with_evaluation_disabled(item) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "eval_status,config",
    [
        # Evaluation switched off: nothing will ever be scored.
        ("DISABLED", {"evaluation": {"enabled": False}}),
        # No published ground truth: also never scored, but evaluation was ON —
        # the shape the first version of this fix broke, because the per-row poll
        # could see "nothing was scored" from the documents and the Executions
        # list could not see it at all.
        ("NO_BASELINE", {"evaluation": {"enabled": True}}),
        # Ordinary scored run, as a control.
        ("COMPLETED", {"evaluation": {"enabled": True}}),
    ],
)
def test_the_badge_rule_gives_every_reader_the_same_answer(eval_status, config):
    """Three resolvers surface the badge and they do not see the same things.

    Only ``get_test_run_status`` reads a run's documents; ``_build_test_run_list``
    and ``get_test_results`` read the run record. So any input to the badge rule
    that is not on the record splits the badge in two — and because the two then
    disagree about whether an aggregation is outstanding, *neither* enqueues one,
    so the split is permanent. Measured, before this assertion existed: a run
    with no published ground truth reported COMPLETE on the poll and EVALUATING
    in the list, with no aggregation ever queued.
    """
    files = ["a.pdf", "b.pdf"]
    table = _run_table("agree-run", files, eval_status, metadata={"Config": config})
    table.metadata["TestRunId"] = "agree-run"

    polled = _status_of("agree-run", table)
    listed = index._build_test_run_list([table.metadata])[0]["status"]

    assert polled["status"] == listed, (
        f"the per-row poll says {polled['status']} and the Executions list says "
        f"{listed} for the same run"
    )


@pytest.mark.unit
def test_the_badge_rule_reads_nothing_but_the_run_record():
    """One mechanism of divergence, closed directly.

    Narrower than it may look, and the test above is what carries the property:
    the divergence actually shipped was an extra *argument*, which this
    assertion cannot see — measured, by reconstructing it. What this closes is
    the other route, a reach past the run record from inside the rule.
    ``_captured_config_of`` is the only such reach in this file; it is used for
    the API's ``evaluationDisabled`` field and for a document carrying no status
    at all, and must never be used for the badge.
    """
    with patch.object(index, "_captured_config_of") as captured:
        assert index._awaiting_metrics({"Status": "RUNNING"}, "RUNNING") is False
        assert index._awaiting_metrics({}, "COMPLETE") is True
        assert (
            index._awaiting_metrics(
                {"testRunResult": {"overallAccuracy": 0.5}}, "COMPLETE"
            )
            is False
        )
        assert (
            index._awaiting_metrics(
                {"Config": {"evaluation": {"enabled": False}}}, "COMPLETE"
            )
            is True
        )
        captured.assert_not_called()


@pytest.mark.unit
def test_the_completed_count_matches_what_the_status_loop_calls_completed():
    """The aborted-run path counts documents separately, and used to count only
    EvaluationStatus=COMPLETED — reporting 0 of 5 completed for a run whose
    documents all finished with no ground truth or with evaluation off, while the
    status loop counted the same documents as completed."""
    files = ["a.pdf", "b.pdf", "c.pdf", "d.pdf", "e.pdf"]
    statuses = ["COMPLETED", "NO_BASELINE", "DISABLED", "FAILED", "RUNNING"]
    responses = {
        "Responses": {
            "tracking": [{"EvaluationStatus": {"S": status}} for status in statuses]
        }
    }
    mock_client = Mock()
    mock_client.batch_get_item.return_value = responses
    mock_table = Mock()
    mock_table.table_name = "tracking"

    with patch.object(index.boto3, "client", return_value=mock_client):
        counted = index._count_completed_documents(mock_table, "run", files)

    # The three settled states, and neither the failure nor the one in flight.
    assert counted == 3


@pytest.mark.unit
def test_the_results_page_is_told_evaluation_was_disabled():
    """So it can say which of the two reasons for having no metrics applies.

    The generic message blames a test set with no published ground truth, which
    is the wrong cause here — the ground truth is there and evaluation was switched
    off in the configuration profile, which is a different screen to go and fix.
    """
    run_id = "off-run"
    mock_table = Mock()
    mock_table.get_item.return_value = {
        "Item": {
            "PK": f"testrun#{run_id}",
            "SK": "metadata",
            "Status": "COMPLETE",
            "FilesCount": 2,
            "CompletedFiles": 2,
            "FailedFiles": 0,
            "Config": {"evaluation": {"enabled": False}},
            "testRunResult": {
                "overallAccuracy": None,
                "weightedOverallScores": {},
                "splitClassificationMetrics": {},
                "confusionMatrix": {},
                "fieldMetrics": {},
                "gradedPacketMetrics": {},
                "excludedDocumentCount": 0,
                "classificationErrors": {},
                "totalCost": 1.25,
            },
        }
    }

    with (
        patch.dict(
            os.environ,
            {
                "TRACKING_TABLE": "tracking",
                "TEST_RESULT_CACHE_UPDATE_QUEUE_URL": "https://sqs.test/q",
            },
        ),
        patch.object(index.dynamodb, "Table", return_value=mock_table),
        patch.object(index, "sqs", Mock()),
        patch.object(index, "_get_test_run_config", return_value={}),
    ):
        result = index.get_test_results(run_id)

    assert result["evaluationDisabled"] is True
    assert result["isDraftLabeling"] is False
    # The cost is real money and is reported either way.
    assert result["totalCost"] == 1.25


@pytest.mark.unit
def test_both_readers_of_an_evaluation_status_agree():
    """Two Lambdas read a document's EvaluationStatus, and both have a default
    that waits.

    The run-status resolver treats an unclassified value as in-flight, which pins
    a run at EVALUATING; the abort resolver treats one as non-terminal, which
    makes it wait out its whole polling budget on a document that finished. The
    same omission therefore shows up twice, with two different symptoms, which is
    how DISABLED and TIMED_OUT came to be missing from both. So the two
    classifications are compared directly: whatever this resolver calls settled or
    unsuccessful, the abort resolver must call terminal.
    """
    abort_path = os.path.join(
        os.path.dirname(__file__),
        *([os.pardir] * 4),
        "nested",
        "api-resolvers",
        "src",
        "lambda",
        "abort_test_runs",
        "index.py",
    )
    with patch("boto3.resource"), patch("boto3.client"):
        abort_spec = importlib.util.spec_from_file_location(
            "abort_test_runs_index", abort_path
        )
        assert abort_spec is not None and abort_spec.loader is not None
        abort_index = importlib.util.module_from_spec(abort_spec)
        abort_spec.loader.exec_module(abort_index)

    terminal_here = index._EVAL_STATUS_SETTLED | index._EVAL_STATUS_UNSUCCESSFUL
    assert terminal_here == abort_index.TERMINAL_EVALUATION_STATUSES

    # And neither of them calls the one in-flight status terminal.
    assert not (index._EVAL_STATUS_IN_FLIGHT & terminal_here)
    assert not (index._EVAL_STATUS_IN_FLIGHT & abort_index.TERMINAL_EVALUATION_STATUSES)


@pytest.mark.unit
@pytest.mark.parametrize(
    "promoted_status", ["BASELINE_COPYING", "BASELINE_AVAILABLE", "BASELINE_ERROR"]
)
def test_promoting_a_document_to_a_baseline_does_not_hold_its_run(promoted_status):
    """A run must not be held by an action taken on it after it finished.

    Promoting a document to an evaluation baseline overwrites the same attribute
    the run's status is derived from, so a run containing a promoted document hit
    the unrecognised-status branch and sat at EVALUATING. ``BASELINE_COPYING`` is
    the one worth being explicit about: it names work in progress, but the copy
    is an async Lambda whose terminal write is best-effort, so an invocation that
    never lands leaves the attribute there with nothing to reconcile it — an
    unbounded wait for something that tells the run nothing either way.
    """
    files = ["a.pdf", "b.pdf"]
    table = _run_table(
        "promoted-run", files, ["COMPLETED", promoted_status], stored_status="RUNNING"
    )
    result = _status_of("promoted-run", table)

    assert table.metadata["Status"] == "COMPLETE"
    assert result["evaluatingFiles"] == 0


@pytest.mark.unit
def test_a_promoted_document_stops_counting_as_a_failed_file():
    """Pinned because it is a real misreport, not because it is right.

    A promotion overwrites the evaluation outcome, so a document whose
    evaluation FAILED and which is then promoted can no longer be told apart
    from one that succeeded: the run moves from PARTIAL_COMPLETE to COMPLETE
    with no failed files, while its cached metrics still cover only the
    documents that were scored. Separating the two needs a second attribute, so
    the alternative on offer is not better accounting but an unbounded wait —
    which is what the run did before #1330. Asserted so the trade-off is visible
    here rather than discovered in a run's figures.
    """
    files = ["a.pdf", "b.pdf", "c.pdf"]
    failed = _run_table("still-failed", files, ["COMPLETED", "FAILED", "COMPLETED"])
    assert _status_of("still-failed", failed)["failedFiles"] == 1
    assert failed.metadata["Status"] == "PARTIAL_COMPLETE"

    promoted = _run_table(
        "promoted-away", files, ["COMPLETED", "BASELINE_AVAILABLE", "COMPLETED"]
    )
    result = _status_of("promoted-away", promoted)

    assert result["failedFiles"] == 0
    assert promoted.metadata["Status"] == "COMPLETE"
