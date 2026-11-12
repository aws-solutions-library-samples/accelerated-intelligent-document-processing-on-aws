# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The test-set copier's pattern mode, driven through its real handler on moto.

"From files in S3" over the Input bucket adds a matching document that has no
ground truth in the evaluation baseline bucket as **unlabeled** — its input is
copied, no ``baseline/`` folder is written — which is how the by-key "From
processed documents" path, zip upload and folder discovery already represent one.
Before, the pattern path dropped such documents, and a create whose every match
was unlabeled ended as a FAILED set the user could not reuse.

The engine here is moto (S3 and DynamoDB), so these assertions are about which
objects the copier writes and which status it records, not about S3 semantics.
"""

import importlib.util
import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

INPUT = "input-bucket"
BASELINE = "baseline-bucket"
TEST_SET = "test-set-bucket"
TRACKING = "tracking"


def _copier_path():
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "src" / "lambda" / "test_set_file_copier" / "index.py"
        if candidate.is_file():
            return candidate
    raise RuntimeError("Could not locate src/lambda/test_set_file_copier")


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("TEST_SET_BUCKET", TEST_SET)
    monkeypatch.setenv("INPUT_BUCKET", INPUT)
    monkeypatch.setenv("BASELINE_BUCKET", BASELINE)
    monkeypatch.setenv("TRACKING_TABLE", TRACKING)
    monkeypatch.delenv("HITL_TABLE_NAME", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        for bucket in (INPUT, BASELINE, TEST_SET):
            s3.create_bucket(Bucket=bucket)
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName=TRACKING,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        # find_matching_files caches a module-level client; one made outside this
        # mock would talk to real S3.
        import idp_common.s3 as idp_s3

        monkeypatch.setattr(idp_s3, "_s3_client", None)
        spec = importlib.util.spec_from_file_location(
            "test_set_file_copier_pattern_index", _copier_path()
        )
        assert spec is not None and spec.loader is not None
        copier = importlib.util.module_from_spec(spec)
        sys.modules["test_set_file_copier_pattern_index"] = copier
        spec.loader.exec_module(copier)
        yield copier, s3


def _put_input(s3, key):
    s3.put_object(Bucket=INPUT, Key=key, Body=b"%PDF")


def _put_baseline(s3, key):
    s3.put_object(
        Bucket=BASELINE, Key=f"{key}/sections/1/result.json", Body=b'{"x": 1}'
    )


def _run(copier, *, set_id="my-set", mode="create", bucket="input", **extra):
    body = {
        "testSetId": set_id,
        "bucketType": bucket,
        "trackingTable": TRACKING,
        "mode": mode,
        **extra,
    }
    copier.handler({"Records": [{"body": json.dumps(body)}]}, None)
    return (
        boto3.resource("dynamodb", region_name="us-east-1")
        .Table(TRACKING)
        .get_item(Key={"PK": f"testset#{set_id}", "SK": "metadata"})["Item"]
    )


def _keys(s3, prefix):
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=TEST_SET, Prefix=prefix)
    return sorted(o["Key"][len(prefix) :] for p in pages for o in p.get("Contents", []))


@pytest.mark.unit
class TestInputBucketPattern:
    def test_labeled_matches_are_copied_with_their_baselines(self, env):
        copier, s3 = env
        _put_input(s3, "a.pdf")
        _put_baseline(s3, "a.pdf")

        row = _run(copier, filePattern="*.pdf")

        assert row["status"] == "COMPLETED"
        assert row["fileCount"] == 1
        assert _keys(s3, "my-set/") == [
            "baseline/a.pdf/sections/1/result.json",
            "input/a.pdf",
        ]

    def test_a_create_whose_every_match_is_unlabeled_succeeds(self, env):
        copier, s3 = env
        _put_input(s3, "a.pdf")
        _put_input(s3, "docs/b.pdf")

        row = _run(copier, filePattern="**/*.pdf")

        assert row["status"] == "COMPLETED", row.get("error")
        assert "error" not in row
        assert row["fileCount"] == 2
        assert _keys(s3, "my-set/") == ["input/a.pdf", "input/docs/b.pdf"]

    def test_a_mixed_create_adds_the_unlabeled_ones_without_baselines(self, env):
        copier, s3 = env
        for key in ("labeled.pdf", "fresh.pdf"):
            _put_input(s3, key)
        _put_baseline(s3, "labeled.pdf")

        row = _run(copier, filePattern="*.pdf")

        assert row["status"] == "COMPLETED"
        assert row["fileCount"] == 2
        assert _keys(s3, "my-set/") == [
            "baseline/labeled.pdf/sections/1/result.json",
            "input/fresh.pdf",
            "input/labeled.pdf",
        ]

    def test_a_mixed_append_adds_unlabeled_documents_to_a_labeled_set(self, env):
        copier, s3 = env
        s3.put_object(Bucket=TEST_SET, Key="my-set/input/old.pdf", Body=b"%PDF")
        s3.put_object(
            Bucket=TEST_SET,
            Key="my-set/baseline/old.pdf/sections/1/result.json",
            Body=b"{}",
        )
        for key in ("labeled.pdf", "fresh.pdf"):
            _put_input(s3, key)
        _put_baseline(s3, "labeled.pdf")

        row = _run(copier, mode="append", filePattern="*.pdf")

        assert row["status"] == "COMPLETED"
        assert row["fileCount"] == 3
        assert _keys(s3, "my-set/") == [
            "baseline/labeled.pdf/sections/1/result.json",
            "baseline/old.pdf/sections/1/result.json",
            "input/fresh.pdf",
            "input/labeled.pdf",
            "input/old.pdf",
        ]

    def test_a_failed_baseline_check_fails_the_job_instead_of_adding_unlabeled(
        self, env
    ):
        """An unreadable baseline bucket must not be read as "no ground truth":
        that would add every labeled document without its labels."""
        copier, s3 = env
        _put_input(s3, "a.pdf")
        _put_baseline(s3, "a.pdf")
        s3.delete_object(Bucket=BASELINE, Key="a.pdf/sections/1/result.json")
        s3.delete_bucket(Bucket=BASELINE)

        row = _run(copier, filePattern="*.pdf")

        assert row["status"] == "FAILED"
        assert _keys(s3, "my-set/") == []

    def test_a_reapplied_pattern_leaves_existing_unlabeled_members_alone(self, env):
        """The append dialog pre-fills the set's own pattern, so it matches the
        set's unlabeled members again. Labels drafted or reviewed in the set were
        made against their current input, which must not be replaced."""
        copier, s3 = env
        s3.put_object(Bucket=TEST_SET, Key="my-set/input/x.pdf", Body=b"OLD")
        s3.put_object(
            Bucket=TEST_SET,
            Key="my-set/baseline/x.pdf/sections/1/result.json",
            Body=b"REVIEWED",
        )
        s3.put_object(Bucket=INPUT, Key="x.pdf", Body=b"NEW")
        _put_input(s3, "fresh.pdf")

        row = _run(copier, mode="append", filePattern="*.pdf")

        assert row["status"] == "COMPLETED", row.get("error")
        assert row["fileCount"] == 2
        body = s3.get_object(Bucket=TEST_SET, Key="my-set/input/x.pdf")["Body"]
        assert body.read() == b"OLD"
        assert _keys(s3, "my-set/") == [
            "baseline/x.pdf/sections/1/result.json",
            "input/fresh.pdf",
            "input/x.pdf",
        ]

    def test_no_match_still_fails_with_a_reason(self, env):
        copier, s3 = env
        _put_input(s3, "a.txt")

        row = _run(copier, filePattern="*.pdf")

        assert row["status"] == "FAILED"
        assert "No files found matching pattern" in row["error"]

    def test_unlabeled_documents_land_exactly_as_the_by_key_path_lands_them(self, env):
        """The downstream readers (label-state probe, document summaries, test
        runs) only see the S3 layout, so matching the by-key path's layout is what
        makes them treat these documents the same way."""
        copier, s3 = env
        for key in ("labeled.pdf", "fresh.pdf"):
            _put_input(s3, key)
        _put_baseline(s3, "labeled.pdf")

        _run(copier, set_id="by-pattern", filePattern="*.pdf")
        _run(
            copier,
            set_id="by-key",
            objectKeys=["labeled.pdf", "fresh.pdf"],
        )

        assert _keys(s3, "by-pattern/") == _keys(s3, "by-key/")
        assert "input/fresh.pdf" in _keys(s3, "by-pattern/")

        from idp_common.review_queues.testset_summaries import summarize_test_set

        docs = {
            d["objectKey"]: d for d in summarize_test_set(s3, TEST_SET, "by-pattern")
        }
        assert set(docs) == {"labeled.pdf", "fresh.pdf"}
        assert docs["fresh.pdf"]["sections"] == []
        assert docs["labeled.pdf"]["sections"] != []


@pytest.mark.unit
class TestTestSetBucketPatternStaysStrict:
    def test_a_missing_baseline_in_the_test_set_bucket_still_fails(self, env):
        copier, s3 = env
        s3.put_object(Bucket=TEST_SET, Key="src/input/a.pdf", Body=b"%PDF")

        row = _run(copier, set_id="copy", bucket="testset", filePattern="src/input/*")

        assert row["status"] == "FAILED"
        assert "Missing baseline folders" in row["error"]
        assert _keys(s3, "copy/") == []

    def test_a_labeled_test_set_bucket_copy_brings_its_baselines(self, env):
        copier, s3 = env
        s3.put_object(Bucket=TEST_SET, Key="src/input/a.pdf", Body=b"%PDF")
        s3.put_object(
            Bucket=TEST_SET, Key="src/baseline/a.pdf/sections/1/result.json", Body=b"{}"
        )

        row = _run(copier, set_id="copy", bucket="testset", filePattern="src/input/*")

        assert row["status"] == "COMPLETED", row.get("error")
        assert _keys(s3, "copy/") == [
            "baseline/a.pdf/sections/1/result.json",
            "input/a.pdf",
        ]

    def test_one_missing_baseline_fails_a_mixed_test_set_bucket_copy(self, env):
        copier, s3 = env
        for name in ("a.pdf", "b.pdf"):
            s3.put_object(Bucket=TEST_SET, Key=f"src/input/{name}", Body=b"%PDF")
        s3.put_object(
            Bucket=TEST_SET, Key="src/baseline/a.pdf/sections/1/result.json", Body=b"{}"
        )

        row = _run(copier, set_id="copy", bucket="testset", filePattern="src/input/*")

        assert row["status"] == "FAILED"
        assert "src/input/b.pdf" in row["error"]
        assert _keys(s3, "copy/") == []
