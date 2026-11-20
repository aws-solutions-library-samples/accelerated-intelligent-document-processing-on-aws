# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The CLI's document-status choices are statuses a tracking record can carry (#2195).

`delete-documents --status-filter` offered `PROCESSING`, which no tracking record
carries, and `status --batch-id` without `--object-status` searched a hand-written
list holding `PROCESSING` and none of the step statuses, so a document mid-pipeline
was left out. The authority here is the writer: `DocumentDynamoDBService` stores
`document.status.value` in `ObjectStatus`, so every offered choice is pushed through
it against a moto table and read back, rather than compared with a list in the test.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import boto3
import click
import pytest
from moto import mock_aws

from idp_cli import search_tracking_table
from idp_cli.cli import cli
from idp_common.dynamodb.client import DynamoDBClient
from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.models import Document, Status

REGION = "us-east-1"
TABLE = "status-choices-tracking"


def _option(command_name, option_name):
    command = cli.commands[command_name]
    return next(p for p in command.params if p.name == option_name)


def _choices(command_name, option_name):
    param_type = _option(command_name, option_name).type
    assert isinstance(param_type, click.Choice), (
        f"{command_name} --{option_name} accepts free text, so a status no record "
        "carries is accepted and matches nothing"
    )
    return list(param_type.choices)


STATUS_OPTIONS = [
    ("delete-documents", "status_filter"),
    ("status", "object_status"),
]


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name=REGION)
        created = resource.create_table(
            TableName=TABLE,
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
        yield created


def _writer():
    return DocumentDynamoDBService(
        dynamodb_client=DynamoDBClient(table_name=TABLE, region=REGION)
    )


def _write(writer, key, status_value):
    """Record a document with this status through the pipeline's own writer."""
    document = Document(
        id=key,
        input_key=key,
        status=Status.QUEUED,
        queued_time="2026-11-20T10:00:00Z",
        initial_event_time="2026-11-20T10:00:00Z",
    )
    writer.create_document(document)
    document.status = Status(status_value)
    writer.update_document(document)


@pytest.mark.parametrize(("command_name", "option_name"), STATUS_OPTIONS)
def test_every_offered_status_is_one_the_writer_records(
    table, command_name, option_name
):
    choices = _choices(command_name, option_name)
    assert choices, "no choices derived: the check below would pass vacuously"
    writer = _writer()
    for index, choice in enumerate(choices):
        _write(writer, f"batch/{index}.pdf", choice)
    stored = sorted(
        item["ObjectStatus"]
        for item in table.scan()["Items"]
        if item["SK"] == "none" and item["PK"].startswith("doc#")
    )
    assert stored == sorted(choices)


@pytest.mark.parametrize(("command_name", "option_name"), STATUS_OPTIONS)
def test_every_recorded_status_is_offered(command_name, option_name):
    """The other direction: no recordable status is missing from the choices.

    `PENDING_UPLOAD` and `IN_PROGRESS` are Jobs API statuses for a job and for a file
    inside a `job#` record; no writer stores either as a document's `ObjectStatus`,
    so offering them would repeat the `PROCESSING` defect.
    """
    job_level = {"PENDING_UPLOAD", "IN_PROGRESS"}
    assert set(_choices(command_name, option_name)) == (
        {s.value for s in Status} - job_level
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["delete-documents", "--stack-name", "s", "--batch-id", "b"],
        ["status", "--stack-name", "s", "--batch-id", "b"],
    ],
)
@pytest.mark.parametrize("value", ["PROCESSING", "PENDING_UPLOAD", "IN_PROGRESS"])
def test_a_status_no_document_carries_is_refused(argv, value):
    from click.testing import CliRunner

    flag = "--status-filter" if argv[0] == "delete-documents" else "--object-status"
    result = CliRunner().invoke(cli, [*argv, flag, value])
    assert result.exit_code == 2
    assert value in result.output


def test_a_search_with_no_status_selects_a_document_in_every_status(table, monkeypatch):
    """`status --batch-id` with no `--object-status` must see in-progress documents."""
    writer = _writer()
    keys = {}
    for status in Status:
        key = f"batch-7/{status.value.lower()}.pdf"
        _write(writer, key, status.value)
        keys[key] = status.value
    # A run record shares the document's PK and carries no status; it must not
    # appear as another copy of the document.
    writer.create_document_run(
        Document(id="batch-7/completed.pdf", input_key="batch-7/completed.pdf"),
        "20261120T090000Z-r1",
        "s3://m/x",
    )
    # A document outside the batch.
    _write(writer, "batch-8/other.pdf", "COMPLETED")

    client = MagicMock()
    client.stack.get_resources.return_value = SimpleNamespace(documents_table=TABLE)
    monkeypatch.setattr(
        search_tracking_table, "IDPClient", MagicMock(return_value=client)
    )
    searcher = search_tracking_table.TrackingTableSearcher("s", region=REGION)

    result = searcher.search_by_pk_and_status("batch-7/")

    assert result["success"] is True
    found = sorted(
        (item["ObjectKey"]["S"], item["ObjectStatus"]["S"]) for item in result["items"]
    )
    assert found == sorted(keys.items())
    assert result["object_status"] == "ALL"
