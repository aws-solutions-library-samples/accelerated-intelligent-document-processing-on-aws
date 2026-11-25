"""Unit tests for the sample-health-insurance-review postRuleValidation hook."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

_HANDLER_DIR = Path(__file__).resolve().parents[1]

_WORKING_BUCKET = "test-working-bucket"
_OUTPUT_BUCKET = "test-output-bucket"
_CLAIMS_TABLE = "TestClaimsStatus"
_DOC_ID = "claims/Prior-Auth-12345678.pdf"
_SUMMARY_KEY = f"{_DOC_ID}/rule_validation/consolidated/consolidated_summary.json"
_SUMMARY_URI = f"s3://{_OUTPUT_BUCKET}/{_SUMMARY_KEY}"


@pytest.fixture
def aws_credentials(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def stack(aws_credentials, monkeypatch):
    """Mocked Working+Output buckets and ClaimsStatus table; yields the module."""
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=_WORKING_BUCKET)
        s3.create_bucket(Bucket=_OUTPUT_BUCKET)
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName=_CLAIMS_TABLE,
            KeySchema=[{"AttributeName": "documentId", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "documentId", "AttributeType": "S"}
            ],
            BillingMode="PAY_PER_REQUEST",
        ).wait_until_exists()

        monkeypatch.setenv("CLAIMS_TABLE_NAME", _CLAIMS_TABLE)
        monkeypatch.setenv("WORKING_BUCKET", _WORKING_BUCKET)
        monkeypatch.setenv("OUTPUT_BUCKET", _OUTPUT_BUCKET)
        sys.path.insert(0, str(_HANDLER_DIR))
        sys.modules.pop("handler", None)
        mod = importlib.import_module("handler")
        sys.path.remove(str(_HANDLER_DIR))
        yield mod


def _summary(counts: dict) -> dict:
    return {
        "document_id": _DOC_ID,
        "overall_status": "COMPLETE",
        "rule_summary": {"global_periods": {"status": "COMPLETE", **counts}},
        "overall_statistics": {
            "total_rules": sum(counts.values()),
            "recommendation_counts": counts,
        },
        "rule_details": {},
        "supporting_pages": ["1", "2"],
    }


def _put_summary(counts: dict) -> None:
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=_OUTPUT_BUCKET,
        Key=_SUMMARY_KEY,
        Body=json.dumps(_summary(counts)).encode("utf-8"),
    )


def _document(output_uri: str | None = _SUMMARY_URI[: -len(".json")] + ".md") -> dict:
    doc = {
        "id": _DOC_ID,
        "input_key": _DOC_ID,
        "output_bucket": _OUTPUT_BUCKET,
        "status": "POSTPROCESSING",
    }
    if output_uri:
        doc["rule_validation_result"] = {
            "output_uri": output_uri,
            "matched_policy_types": ["global_periods"],
        }
    return doc


def _event(document: dict) -> dict:
    return {
        "hookPoint": "postRuleValidation",
        "featureId": "sample-health-insurance-review",
        "document": document,
        "executionArn": "arn:aws:states:us-east-1:111:execution:wf:run-1",
    }


def _get_row():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    return ddb.Table(_CLAIMS_TABLE).get_item(Key={"documentId": _DOC_ID}).get("Item")


# ---------------------------------------------------------------------------
# Status derivation matrix (pure function)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "counts,expected",
    [
        ({"Pass": 5}, "CLEAN_CLAIM"),  # nosec B105 - rule counter fixture
        ({"Pass": 4, "Fail": 1}, "REVIEW_REQUIRED"),  # nosec B105 - rule counter fixture
        ({"Fail": 2, "Information Not Found": 3}, "REVIEW_REQUIRED"),
        ({"Pass": 1, "Information Not Found": 4}, "INSUFFICIENT_DOCUMENTATION"),  # nosec B105 - rule counter fixture
        ({"Information Not Found": 5}, "INSUFFICIENT_DOCUMENTATION"),
        ({"Pass": 3, "Unknown": 1}, "INSUFFICIENT_DOCUMENTATION"),  # nosec B105 - rule counter fixture
        ({}, "INSUFFICIENT_DOCUMENTATION"),
    ],
)
def test_derive_status_matrix(stack, counts, expected):
    status, _ = stack.derive_status(counts)
    assert status == expected


def test_derive_status_ignores_zero_and_non_numeric_counts(stack):
    status, normalized = stack.derive_status(
        {"Pass": 3, "Fail": 0, "Information Not Found": "oops"}  # nosec B105 - rule counter fixture
    )
    assert status == "CLEAN_CLAIM"
    assert normalized == {"Pass": 3}  # nosec B105 - rule counter fixture


# ---------------------------------------------------------------------------
# End-to-end handler behaviour
# ---------------------------------------------------------------------------
def test_clean_claim_written_to_table(stack):
    _put_summary({"Pass": 7})  # nosec B105 - rule counter fixture
    result = stack.lambda_handler(_event(_document()), None)

    assert result["status"] == "CLEAN_CLAIM"
    row = _get_row()
    assert row["status"] == "CLEAN_CLAIM"
    assert row["passCount"] == 7
    assert row["failCount"] == 0
    assert row["policyTypes"] == ["global_periods"]
    assert row["summaryJsonUri"] == _SUMMARY_URI
    assert row["summaryMdUri"] == _SUMMARY_URI[: -len(".json")] + ".md"
    assert row["executionArn"].endswith("run-1")


def test_the_row_says_whether_the_document_was_reviewed_and_what_run_wrote_it(stack):
    _put_summary({"Pass": 7})  # nosec B105 - rule counter fixture
    event = {**_event(_document()), "runKind": "new_upload", "reviewed": False}

    stack.lambda_handler(event, None)

    row = _get_row()
    assert row["reviewed"] is False
    assert row["runKind"] == "new_upload"


def test_the_run_after_a_review_replaces_the_row_written_before_it(stack):
    """A held document reaches the hook twice. The run after the completed
    review validates the corrected values, and its row replaces the first."""
    _put_summary({"Pass": 4, "Fail": 3})  # nosec B105 - rule counter fixture
    stack.lambda_handler(
        {**_event(_document()), "runKind": "new_upload", "reviewed": False}, None
    )
    before = _get_row()
    assert (before["status"], before["reviewed"]) == ("REVIEW_REQUIRED", False)

    _put_summary({"Pass": 7})  # nosec B105 - rule counter fixture
    after_review = {
        **_event(_document()),
        "executionArn": "arn:aws:states:us-east-1:111:execution:wf:run-2",
        "runKind": "review_completed",
        "reviewed": True,
    }
    stack.lambda_handler(after_review, None)

    row = _get_row()
    assert (row["status"], row["reviewed"], row["runKind"]) == (
        "CLEAN_CLAIM",
        True,
        "review_completed",
    )
    assert row["failCount"] == 0
    assert row["executionArn"].endswith("run-2")


@pytest.mark.parametrize(
    "more", [{}, {"runKind": None, "reviewed": None}, {"runKind": ""}]
)
def test_an_event_without_the_run_fields_is_not_reviewed_and_unplanned(stack, more):
    """An older host's dispatcher sends neither field."""
    _put_summary({"Pass": 7})  # nosec B105 - rule counter fixture

    stack.lambda_handler({**_event(_document()), **more}, None)

    row = _get_row()
    assert row["reviewed"] is False
    assert row["runKind"] == "unplanned"


def test_failed_rules_mark_review_required(stack):
    _put_summary({"Pass": 4, "Fail": 2, "Information Not Found": 1})  # nosec B105 - rule counter fixture
    result = stack.lambda_handler(_event(_document()), None)
    assert result["status"] == "REVIEW_REQUIRED"
    assert _get_row()["notFoundCount"] == 1


def test_compressed_document_reference_is_resolved(stack):
    """The dispatcher usually passes {"compressed": true, "s3_uri": ...}."""
    _put_summary({"Pass": 2})  # nosec B105 - rule counter fixture
    full_doc_key = f"compressed_documents/{_DOC_ID}/123_hook_state.json"
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=_WORKING_BUCKET,
        Key=full_doc_key,
        Body=json.dumps(_document()).encode("utf-8"),
    )
    compressed_ref = {
        "compressed": True,
        "s3_uri": f"s3://{_WORKING_BUCKET}/{full_doc_key}",
        "document_id": _DOC_ID,
    }
    result = stack.lambda_handler(_event(compressed_ref), None)
    assert result["status"] == "CLEAN_CLAIM"
    assert _get_row() is not None


def test_falls_back_to_conventional_path_without_result_block(stack):
    """Docs without rule_validation_result still resolve via output_bucket."""
    _put_summary({"Pass": 1, "Fail": 1})  # nosec B105 - rule counter fixture
    result = stack.lambda_handler(_event(_document(output_uri=None)), None)
    assert result["status"] == "REVIEW_REQUIRED"


def test_missing_summary_skips_without_raising(stack):
    result = stack.lambda_handler(_event(_document()), None)
    assert result["skipped"] is True
    assert _get_row() is None


def test_unresolvable_document_skips_without_raising(stack):
    result = stack.lambda_handler(_event({"compressed": True}), None)
    assert result["skipped"] is True


def test_no_policy_match_records_insufficient_claim(stack):
    """A NO_POLICY_MATCH run (rule validation skipped for lack of a matching
    policy class) still writes a consolidated summary with zero rules. The
    workflow now routes that skip path through the postRuleValidation hook, so
    the claim must land as INSUFFICIENT_DOCUMENTATION rather than never
    appearing in the Claims dashboard."""
    boto3.client("s3", region_name="us-east-1").put_object(
        Bucket=_OUTPUT_BUCKET,
        Key=_SUMMARY_KEY,
        Body=json.dumps(
            {
                "document_id": _DOC_ID,
                "overall_status": "NO_POLICY_MATCH",
                "total_policy_types": 0,
                "rule_summary": {},
                "overall_statistics": {
                    "total_rules": 0,
                    "pass_count": 0,  # nosec B105 - statistics key, not a password
                    "fail_count": 0,
                    "information_not_found_count": 0,
                },
                "rule_details": {},
                "supporting_pages": [],
            }
        ).encode("utf-8"),
    )
    # Skip-path document carries no rule_validation_result block; the hook
    # falls back to the conventional output path.
    result = stack.lambda_handler(_event(_document(output_uri=None)), None)
    assert result["status"] == "INSUFFICIENT_DOCUMENTATION"
    row = _get_row()
    assert row["status"] == "INSUFFICIENT_DOCUMENTATION"
    assert row["totalRules"] == 0
    assert row["policyTypes"] == []


def test_rerun_is_idempotent_overwrite(stack):
    _put_summary({"Pass": 3})  # nosec B105 - rule counter fixture
    stack.lambda_handler(_event(_document()), None)
    assert _get_row()["status"] == "CLEAN_CLAIM"

    # Rule validation re-ran and found a failure; the hook overwrites.
    _put_summary({"Pass": 2, "Fail": 1})  # nosec B105 - rule counter fixture
    stack.lambda_handler(_event(_document()), None)
    row = _get_row()
    assert row["status"] == "REVIEW_REQUIRED"
    assert row["failCount"] == 1
