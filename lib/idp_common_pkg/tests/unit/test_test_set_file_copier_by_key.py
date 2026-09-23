# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The test-set copier's by-key mode: documents selected on the Document List.

Loads ``src/lambda/test_set_file_copier/index.py`` and replaces its module-level
S3 and DynamoDB handles with in-memory fakes, so the assertions are about what
the copier writes, not about boto3.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_copier():
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "src" / "lambda" / "test_set_file_copier" / "index.py"
        if candidate.is_file():
            break
    else:
        raise RuntimeError("Could not locate src/lambda/test_set_file_copier")
    spec = importlib.util.spec_from_file_location(
        "test_set_file_copier_index", candidate
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["test_set_file_copier_index"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeS3:
    """Input bucket holds ``inputs``; baseline bucket holds ``baselines`` (key -> files)."""

    def __init__(self, inputs, baselines):
        self.inputs = set(inputs)
        self.baselines = baselines
        self.copied = []
        self.test_set_objects = set()

    def head_object(self, Bucket, Key):
        if Bucket == "input-bucket" and Key in self.inputs:
            return {}
        raise Exception("404 Not Found")

    def _objects(self, Bucket, Prefix):
        if Bucket == "baseline-bucket":
            for key, files in self.baselines.items():
                for f in files:
                    full = f"{key}/{f}"
                    if full.startswith(Prefix):
                        yield full
        elif Bucket == "test-set-bucket":
            for key in sorted(self.test_set_objects):
                if key.startswith(Prefix):
                    yield key

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=1000):
        contents = [{"Key": k} for k in self._objects(Bucket, Prefix)][:MaxKeys]
        return {"Contents": contents} if contents else {}

    def get_paginator(self, name):
        fake = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                contents = [{"Key": k} for k in fake._objects(Bucket, Prefix)]
                return [{"Contents": contents}] if contents else [{}]

        return Paginator()

    def copy_object(self, CopySource, Bucket, Key):
        self.copied.append((CopySource["Bucket"], CopySource["Key"], Bucket, Key))
        self.test_set_objects.add(Key)


class FakeTable:
    def __init__(self):
        self.updates = []

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


@pytest.fixture
def copier(monkeypatch):
    monkeypatch.setenv("TEST_SET_BUCKET", "test-set-bucket")
    monkeypatch.setenv("INPUT_BUCKET", "input-bucket")
    monkeypatch.setenv("BASELINE_BUCKET", "baseline-bucket")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    return _load_copier()


def _event(body):
    return {"Records": [{"body": json.dumps(body)}]}


def _by_key_message(keys):
    return {
        "testSetId": "my-set",
        "objectKeys": keys,
        "bucketType": "input",
        "trackingTable": "tracking",
        "mode": "append",
    }


def _wire(monkeypatch, copier, s3, table=None):
    table = table or FakeTable()
    monkeypatch.setattr(copier, "s3", s3)

    class Dynamo:
        def Table(self, name):
            return table

    monkeypatch.setattr(copier, "dynamodb", Dynamo())
    return table


def _final_update(table):
    assert table.updates, "copier wrote no status"
    return table.updates[-1]


@pytest.mark.unit
class TestByKeyMode:
    def test_labeled_and_unlabeled_documents_are_both_added(self, monkeypatch, copier):
        s3 = FakeS3(
            inputs={"labeled.pdf", "fresh.pdf"},
            baselines={"labeled.pdf": ["sections/1/result.json"]},
        )
        table = _wire(monkeypatch, copier, s3)

        copier.handler(_event(_by_key_message(["labeled.pdf", "fresh.pdf"])), None)

        input_copies = {c[3] for c in s3.copied if "/input/" in c[3]}
        baseline_copies = {c[3] for c in s3.copied if "/baseline/" in c[3]}
        assert input_copies == {"my-set/input/labeled.pdf", "my-set/input/fresh.pdf"}
        assert baseline_copies == {"my-set/baseline/labeled.pdf/sections/1/result.json"}

        final = _final_update(table)
        assert final["ExpressionAttributeValues"][":status"] == "COMPLETED"
        assert final["ExpressionAttributeValues"][":count"] == 2
        assert "labelState" not in final["UpdateExpression"]
        assert "REMOVE lastAddResult, contentSignature" in final["UpdateExpression"]

    def test_a_key_that_no_longer_exists_is_skipped_not_fatal(
        self, monkeypatch, copier
    ):
        s3 = FakeS3(inputs={"here.pdf"}, baselines={})
        table = _wire(monkeypatch, copier, s3)

        copier.handler(_event(_by_key_message(["here.pdf", "gone.pdf"])), None)

        assert {c[3] for c in s3.copied} == {"my-set/input/here.pdf"}
        final = _final_update(table)
        assert final["ExpressionAttributeValues"][":status"] == "COMPLETED"
        assert final["ExpressionAttributeValues"][":count"] == 1

    def test_all_keys_missing_fails_the_job_with_a_reason(self, monkeypatch, copier):
        s3 = FakeS3(inputs=set(), baselines={})
        table = _wire(monkeypatch, copier, s3)

        copier.handler(_event(_by_key_message(["gone.pdf"])), None)

        assert s3.copied == []
        final = _final_update(table)
        assert final["ExpressionAttributeValues"][":status"] == "FAILED"
        assert (
            "None of the 1 selected documents exist"
            in (final["ExpressionAttributeValues"][":error"])
        )

    def test_by_key_is_only_offered_from_the_input_bucket(self, monkeypatch, copier):
        s3 = FakeS3(inputs={"a.pdf"}, baselines={})
        table = _wire(monkeypatch, copier, s3)
        message = _by_key_message(["a.pdf"])
        message["bucketType"] = "testset"

        copier.handler(_event(message), None)

        final = _final_update(table)
        assert final["ExpressionAttributeValues"][":status"] == "FAILED"
        assert "input bucket" in final["ExpressionAttributeValues"][":error"]

    def test_nested_keys_keep_their_path_under_input(self, monkeypatch, copier):
        s3 = FakeS3(
            inputs={"batch-3/doc.pdf"},
            baselines={"batch-3/doc.pdf": ["sections/1/result.json"]},
        )
        _wire(monkeypatch, copier, s3)

        copier.handler(_event(_by_key_message(["batch-3/doc.pdf"])), None)

        assert (
            "input-bucket",
            "batch-3/doc.pdf",
            "test-set-bucket",
            "my-set/input/batch-3/doc.pdf",
        ) in s3.copied
        assert (
            "baseline-bucket",
            "batch-3/doc.pdf/sections/1/result.json",
            "test-set-bucket",
            "my-set/baseline/batch-3/doc.pdf/sections/1/result.json",
        ) in s3.copied


@pytest.mark.unit
class TestPatternModeIsUnchanged:
    def test_pattern_append_still_drops_unlabeled_documents(self, monkeypatch, copier):
        s3 = FakeS3(
            inputs={"labeled.pdf", "fresh.pdf"},
            baselines={"labeled.pdf": ["sections/1/result.json"]},
        )
        table = _wire(monkeypatch, copier, s3)
        monkeypatch.setattr(
            copier, "find_matching_files", lambda *a, **k: ["fresh.pdf", "labeled.pdf"]
        )

        copier.handler(
            _event(
                {
                    "testSetId": "my-set",
                    "filePattern": "*.pdf",
                    "bucketType": "input",
                    "trackingTable": "tracking",
                    "mode": "append",
                }
            ),
            None,
        )

        assert {c[3] for c in s3.copied if "/input/" in c[3]} == {
            "my-set/input/labeled.pdf"
        }
        final = _final_update(table)
        assert final["ExpressionAttributeValues"][":status"] == "COMPLETED"
        assert final["ExpressionAttributeValues"][":count"] == 1


@pytest.mark.unit
def test_result_message_names_the_unlabeled_and_missing_counts(copier):
    assert copier._selected_documents_result(3, 0, 0) == "Added 3 files"
    assert copier._selected_documents_result(3, 2, 0) == "Added 3 files (2 unlabeled)"
    assert (
        copier._selected_documents_result(2, 1, 1)
        == "Added 2 files (1 unlabeled, 1 not found)"
    )
