# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reprocess carries the document's caller-supplied attributes forward (#1851).

Reprocess builds a fresh document and PUTs a new tracking row over the old one,
so anything it does not copy is lost. The attributes are the intake snapshot on
the old row; only when there is no row are they read from the object again, by
the same rules intake uses (against moto's S3 here, with real user metadata).
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("INPUT_BUCKET", "test-in")
os.environ.setdefault("OUTPUT_BUCKET", "test-out")
os.environ.setdefault("QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/1/test-queue")
os.environ.setdefault("TRACKING_TABLE", "test-tracking")


def _load_index():
    for name in list(sys.modules):
        if name == "idp_common" or name.startswith("idp_common."):
            if isinstance(sys.modules[name], MagicMock):
                del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        "reprocess_resolver_index_attributes", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


index = _load_index()

from idp_common import document_attributes as da  # noqa: E402
from idp_common.models import (  # noqa: E402
    Document,
    ProcessingIssue,
    Status,
    attributes_dropped_issue,
)


def _reprocess(row, s3_client):
    with (
        patch.object(index, "_refuse_if_refused_by_the_malware_gate", return_value=row),
        patch.object(index, "s3_client", s3_client),
        patch.object(index, "input_bucket", "test-in"),
        patch.object(index, "_withdraw_review"),
        patch.object(index, "_delete_output_data"),
        patch.object(index.document_service, "create_document") as create,
        patch.object(
            index.sqs_client, "send_message", return_value={"MessageId": "m"}
        ) as send,
    ):
        index.reprocess_document("claims/a.pdf")
    created = create.call_args.args[0]
    sent = json.loads(send.call_args.kwargs["MessageBody"])
    return created, sent


def _bucket():
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="test-in")
    return s3


@pytest.mark.unit
@mock_aws
def test_the_rows_attributes_and_drop_warning_are_carried_forward():
    s3 = _bucket()
    # The object's metadata now says something else; the row's snapshot wins.
    s3.put_object(
        Bucket="test-in",
        Key="claims/a.pdf",
        Body=b"%PDF",
        Metadata={"idp-attr-account-id": "CHANGED"},
    )
    dropped = attributes_dropped_issue([("Bad", "key rule")])
    other = ProcessingIssue(
        stage="ingest", severity="warning", code="other", message="m"
    )
    row = Document(
        id="claims/a.pdf",
        input_key="claims/a.pdf",
        status=Status.COMPLETED,
        attributes={"account_id": "ACME-0042", "amount": 3},
        processing_issues=[dropped, other],
    )
    created, sent = _reprocess(row, s3)
    assert created.attributes == {"account_id": "ACME-0042", "amount": 3}
    assert [i.code for i in created.processing_issues] == [da.ISSUE_ATTRIBUTES_DROPPED]
    assert sent["attributes"] == {"account_id": "ACME-0042", "amount": 3}


@pytest.mark.unit
@mock_aws
def test_without_a_row_they_are_read_from_the_object_by_the_intake_rules():
    s3 = _bucket()
    s3.put_object(
        Bucket="test-in",
        Key="claims/a.pdf",
        Body=b"%PDF",
        Metadata={"idp-attr-account-id": "ACME-0042", "idp-attr-bad-": "x" * 2000},
    )
    created, sent = _reprocess(None, s3)
    assert created.attributes == {"account_id": "ACME-0042"}
    (issue,) = created.processing_issues
    assert issue.code == da.ISSUE_ATTRIBUTES_DROPPED


@pytest.mark.unit
@mock_aws
def test_a_document_without_attributes_reprocesses_exactly_as_before():
    s3 = _bucket()
    s3.put_object(Bucket="test-in", Key="claims/a.pdf", Body=b"%PDF")
    row = Document(id="claims/a.pdf", input_key="claims/a.pdf", status=Status.COMPLETED)
    created, sent = _reprocess(row, s3)
    assert created.attributes == {}
    assert created.processing_issues == []
    assert "attributes" not in sent and "processing_issues" not in sent
