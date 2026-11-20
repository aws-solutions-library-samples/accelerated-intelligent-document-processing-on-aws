# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""How the reporting Glue tables' columns are compared and updated (GitHub #2110).

Two properties of ``SaveReportingData``'s on-demand Glue tables:

* An unchanged schema causes no ``UpdateTable``. Glue stores column names
  lowercased, while the columns built from a document keep the extraction
  schema's casing (``inference_result.Account Holder``), so the comparison has to
  be on the lowercased name. Compared as written, every mixed-case field looked
  new and every save rewrote the table and archived a table version.
* An update never removes a column. ``UpdateTable`` replaces the column list
  wholesale, so sending only the current document's columns dropped every column
  an earlier document of the same class contributed.

The Glue backend is moto behind a thin wrapper that can lowercase column names on
the way in, the way Glue does; moto itself stores them as given. Every test that
depends on the store's casing runs against both, because the comparison must be
right whichever way the names come back.
"""

import os
from unittest.mock import MagicMock, patch

import boto3
import pyarrow as pa
import pytest
from moto import mock_aws

from idp_common.reporting.save_reporting_data import SaveReportingData

pytestmark = pytest.mark.unit

DATABASE = "reporting_db"
SECTION = "BankStatement"
TABLE = "document_sections_bankstatement"


class RecordingGlue:
    """A moto Glue client that records ``update_table`` calls.

    With ``lowercase=True`` it lowercases column names on create and update, which
    is what Glue does to a column name it stores.
    """

    def __init__(self, client, *, lowercase: bool):
        self._client = client
        self._lowercase = lowercase
        self.update_inputs = []

    def _stored(self, table_input):
        if not self._lowercase:
            return table_input
        descriptor = dict(table_input["StorageDescriptor"])
        descriptor["Columns"] = [
            {**col, "Name": col["Name"].lower()} for col in descriptor["Columns"]
        ]
        return {**table_input, "StorageDescriptor": descriptor}

    def get_table(self, **kwargs):
        return self._client.get_table(**kwargs)

    def create_table(self, *, DatabaseName, TableInput):
        return self._client.create_table(
            DatabaseName=DatabaseName, TableInput=self._stored(TableInput)
        )

    def update_table(self, *, DatabaseName, TableInput):
        self.update_inputs.append(TableInput)
        return self._client.update_table(
            DatabaseName=DatabaseName, TableInput=self._stored(TableInput)
        )

    def stored_columns(self, table=TABLE):
        return self._client.get_table(DatabaseName=DATABASE, Name=table)["Table"][
            "StorageDescriptor"
        ]["Columns"]

    def version_count(self, table=TABLE):
        return len(
            self._client.get_table_versions(DatabaseName=DATABASE, TableName=table)[
                "TableVersions"
            ]
        )


@pytest.fixture
def aws_env():
    with patch.dict(
        os.environ,
        {
            "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
            "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - moto placeholder
            "AWS_DEFAULT_REGION": "us-east-1",
        },
    ):
        with mock_aws():
            client = boto3.client("glue", region_name="us-east-1")
            client.create_database(DatabaseInput={"Name": DATABASE})
            yield client


def make_saver(glue_client):
    with patch("boto3.client", return_value=MagicMock()):
        saver = SaveReportingData(reporting_bucket="reporting-bucket")
    saver.glue_client = glue_client
    saver.database_name = DATABASE
    return saver


@pytest.fixture(params=[True, False], ids=["glue-lowercases", "store-keeps-case"])
def glue(request, aws_env):
    return RecordingGlue(aws_env, lowercase=request.param)


@pytest.fixture
def lowercasing_glue(aws_env):
    return RecordingGlue(aws_env, lowercase=True)


def section_schema(saver, inference_result):
    """The schema the save path builds for a section, through the real code."""
    record = saver._flatten_json_data(
        {
            "document_id": "doc-1",
            "section_id": "1",
            "section_classification": SECTION,
            "timestamp": "2025-01-01T00:00:00",
            "inference_result": inference_result,
        }
    )
    return saver._create_dynamic_schema([record])


MIXED_CASE_FIELDS = {
    "Account Holder": "Jane Doe",
    "AccountNumber": "123",
    "statement_period": "Jan",
}


class TestSectionTable:
    def test_an_unchanged_mixed_case_schema_does_not_update_the_table(self, glue):
        saver = make_saver(glue)
        schema = section_schema(saver, MIXED_CASE_FIELDS)
        assert saver._create_or_update_glue_table(SECTION, schema) is True
        versions = glue.version_count()

        for _ in range(3):
            assert saver._create_or_update_glue_table(SECTION, schema) is False

        assert glue.update_inputs == []
        assert glue.version_count() == versions

    def test_a_new_column_is_one_update_carrying_the_existing_and_the_new(self, glue):
        saver = make_saver(glue)
        saver._create_or_update_glue_table(
            SECTION, section_schema(saver, MIXED_CASE_FIELDS)
        )
        before = glue.stored_columns()

        widened = {**MIXED_CASE_FIELDS, "Closing Balance": "10.00"}
        assert (
            saver._create_or_update_glue_table(SECTION, section_schema(saver, widened))
            is True
        )

        assert len(glue.update_inputs) == 1
        sent = glue.update_inputs[0]["StorageDescriptor"]["Columns"]
        assert sent == before + [
            {"Name": "inference_result.Closing Balance", "Type": "string"}
        ]

    def test_a_column_the_current_document_lacks_is_kept(self, glue):
        # UpdateTable replaces the column list, so an update built from one
        # document alone drops what earlier documents of the class contributed.
        saver = make_saver(glue)
        saver._create_or_update_glue_table(
            SECTION, section_schema(saver, MIXED_CASE_FIELDS)
        )
        earlier = {c["Name"].lower() for c in glue.stored_columns()}

        other_document = {"Closing Balance": "10.00"}
        saver._create_or_update_glue_table(
            SECTION, section_schema(saver, other_document)
        )

        stored = {c["Name"].lower() for c in glue.stored_columns()}
        assert earlier <= stored
        assert "inference_result.closing balance" in stored

    def test_repeated_saves_do_not_grow_the_column_list(self, glue):
        # A merge that compared names as written would append the mixed-case
        # names again on every save once Glue had lowercased them.
        saver = make_saver(glue)
        first = section_schema(saver, MIXED_CASE_FIELDS)
        second = section_schema(saver, {**MIXED_CASE_FIELDS, "Extra": "x"})
        saver._create_or_update_glue_table(SECTION, first)
        saver._create_or_update_glue_table(SECTION, second)
        settled = len(glue.stored_columns())

        for schema in (first, second, first, second):
            saver._create_or_update_glue_table(SECTION, schema)

        assert len(glue.stored_columns()) == settled
        assert len(glue.update_inputs) == 1

    def test_a_name_differing_only_in_case_is_the_same_column(self, lowercasing_glue):
        saver = make_saver(lowercasing_glue)
        saver._create_or_update_glue_table(
            SECTION, section_schema(saver, {"Account Holder": "a"})
        )
        assert (
            saver._create_or_update_glue_table(
                SECTION, section_schema(saver, {"ACCOUNT HOLDER": "a"})
            )
            is False
        )
        assert lowercasing_glue.update_inputs == []

    def test_a_changed_type_is_updated_to_the_type_the_parquet_carries(
        self, lowercasing_glue
    ):
        # The save path writes `timestamp` as a parquet timestamp. A table whose
        # column says otherwise (an older release, or a hand edit) is corrected
        # to the written type, in place, without moving or dropping any column.
        saver = make_saver(lowercasing_glue)
        schema = section_schema(saver, MIXED_CASE_FIELDS)
        saver._create_or_update_glue_table(SECTION, schema)
        table = lowercasing_glue.get_table(DatabaseName=DATABASE, Name=TABLE)["Table"]
        drifted = [
            {**c, "Type": "string"} if c["Name"] == "timestamp" else c
            for c in table["StorageDescriptor"]["Columns"]
        ]
        lowercasing_glue._client.update_table(
            DatabaseName=DATABASE,
            TableInput={
                "Name": TABLE,
                "StorageDescriptor": {
                    **{
                        k: v
                        for k, v in table["StorageDescriptor"].items()
                        if k != "Columns"
                    },
                    "Columns": drifted,
                },
                "PartitionKeys": table["PartitionKeys"],
                "TableType": table["TableType"],
                "Parameters": table["Parameters"],
            },
        )

        assert saver._create_or_update_glue_table(SECTION, schema) is True

        assert len(lowercasing_glue.update_inputs) == 1
        sent = lowercasing_glue.update_inputs[0]["StorageDescriptor"]["Columns"]
        assert [c["Name"] for c in sent] == [c["Name"] for c in drifted]
        assert {"Name": "timestamp", "Type": "timestamp"} in sent
        assert saver._create_or_update_glue_table(SECTION, schema) is False

    def test_a_location_change_still_updates_and_keeps_every_column(
        self, lowercasing_glue
    ):
        saver = make_saver(lowercasing_glue)
        saver._create_or_update_glue_table(
            SECTION, section_schema(saver, MIXED_CASE_FIELDS)
        )
        before = lowercasing_glue.stored_columns()

        saver.reporting_bucket = "moved-bucket"
        assert (
            saver._create_or_update_glue_table(
                SECTION, section_schema(saver, {"Account Holder": "a"})
            )
            is True
        )

        sent = lowercasing_glue.update_inputs[0]["StorageDescriptor"]
        assert sent["Location"] == "s3://moved-bucket/document_sections/bankstatement/"
        assert sent["Columns"] == before

    def test_names_colliding_by_case_in_one_document_do_not_update_each_save(
        self, lowercasing_glue
    ):
        # A list-shaped extraction puts item keys at the top level, so one
        # document can carry `Timestamp` (a string) beside the save path's own
        # `timestamp`. The first rewrites the stored type and the second puts it
        # back; the table is unchanged and must not be updated.
        saver = make_saver(lowercasing_glue)
        schema = pa.schema(
            [
                ("Timestamp", pa.string()),
                ("document_id", pa.string()),
                ("timestamp", pa.timestamp("ms")),
            ]
        )
        saver._create_or_update_glue_table(SECTION, schema)
        for _ in range(3):
            assert saver._create_or_update_glue_table(SECTION, schema) is False
        assert lowercasing_glue.update_inputs == []


class TestMergeGlueColumns:
    def test_existing_order_and_column_metadata_are_preserved(self):
        existing = [
            {
                "Name": "b",
                "Type": "string",
                "Comment": "kept",
                "Parameters": {"k": "v"},
            },
            {"Name": "a", "Type": "string"},
        ]
        merged, changed = SaveReportingData._merge_glue_columns(
            existing, [{"Name": "A", "Type": "string"}, {"Name": "B", "Type": "string"}]
        )
        assert changed is False
        assert merged == existing

    def test_an_existing_column_without_a_type_is_not_a_change(self):
        merged, changed = SaveReportingData._merge_glue_columns(
            [{"Name": "a"}], [{"Name": "A", "Type": "string"}]
        )
        assert changed is False
        assert merged == [{"Name": "a"}]

    def test_type_comparison_ignores_case(self):
        _, changed = SaveReportingData._merge_glue_columns(
            [{"Name": "a", "Type": "STRING"}], [{"Name": "a", "Type": "string"}]
        )
        assert changed is False

    def test_a_type_rewritten_and_restored_is_not_a_change(self):
        existing = [{"Name": "timestamp", "Type": "timestamp"}]
        merged, changed = SaveReportingData._merge_glue_columns(
            existing,
            [
                {"Name": "Timestamp", "Type": "string"},
                {"Name": "timestamp", "Type": "timestamp"},
            ],
        )
        assert changed is False
        assert merged == existing

    def test_an_appended_column_is_a_copy(self):
        new = [{"Name": "b", "Type": "string"}, {"Name": "B", "Type": "bigint"}]
        merged, _ = SaveReportingData._merge_glue_columns([], new)
        assert merged == [{"Name": "b", "Type": "bigint"}]
        assert new[0] == {"Name": "b", "Type": "string"}

    def test_the_inputs_are_not_mutated(self):
        existing = [{"Name": "a", "Type": "string"}]
        new = [{"Name": "a", "Type": "bigint"}, {"Name": "b", "Type": "string"}]
        merged, changed = SaveReportingData._merge_glue_columns(existing, new)
        assert changed is True
        assert merged == [
            {"Name": "a", "Type": "bigint"},
            {"Name": "b", "Type": "string"},
        ]
        assert existing == [{"Name": "a", "Type": "string"}]
        assert new == [
            {"Name": "a", "Type": "bigint"},
            {"Name": "b", "Type": "string"},
        ]


RULE_SCHEMA = pa.schema(
    [
        ("document_id", pa.string()),
        ("Policy Type", pa.string()),
        ("validation_date", pa.timestamp("ms")),
    ]
)


class TestRuleValidationTable:
    def test_an_unchanged_schema_does_not_update_the_table(self, glue):
        saver = make_saver(glue)
        saver._create_or_update_rule_validation_glue_table("rule_t", RULE_SCHEMA)
        for _ in range(3):
            saver._create_or_update_rule_validation_glue_table("rule_t", RULE_SCHEMA)
        assert glue.update_inputs == []
        assert glue.version_count("rule_t") == 1

    def test_a_new_column_keeps_the_columns_already_there(self, glue):
        saver = make_saver(glue)
        saver._create_or_update_rule_validation_glue_table("rule_t", RULE_SCHEMA)
        before = glue.stored_columns("rule_t")

        narrower_plus_one = pa.schema(
            [("document_id", pa.string()), ("Reasoning", pa.string())]
        )
        assert (
            saver._create_or_update_rule_validation_glue_table(
                "rule_t", narrower_plus_one
            )
            is True
        )

        assert len(glue.update_inputs) == 1
        assert glue.update_inputs[0]["StorageDescriptor"]["Columns"] == before + [
            {"Name": "Reasoning", "Type": "string"}
        ]
