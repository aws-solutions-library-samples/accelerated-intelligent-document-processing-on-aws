# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""listDocuments reads TypeDateIndexV2, and falls back to TypeDateIndex until it can.

TypeDateIndex cannot carry the per-document counts the document list shows
(`MissingRequiredFieldCount`, `ProcessingIssueCount`, and `ConfigRevision`), and
DynamoDB cannot widen an existing index's projection. So the tracking table gains a
second index with the same keys and a wider projection, and the list reads it.

A GSI added by a stack update is not queryable until DynamoDB has backfilled it, so
for a while after an upgrade the list is served from the old index instead. These
tests cover both halves against the real index definitions: every table here is
built from the TrackingTable resource in `template.yaml`, not from a copy of it, so
the projection under test is the one that deploys. The queries run against moto's
DynamoDB, whose projection and pagination behaviour is moto's, not DynamoDB's. An
unreadable index cannot be produced faithfully by moto — it creates every index
ACTIVE, and answers a query on an absent one with a different error code than
DynamoDB — so those cases inject DynamoDB's own error, recorded from live queries.
"""

import ast
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
import yaml
from botocore.exceptions import ClientError
from moto import mock_aws

REPO_ROOT = Path(__file__).resolve().parents[5]
TABLE = "IDP-TrackingTable-list-index"
OLD_INDEX = "TypeDateIndex"
NEW_INDEX = "TypeDateIndexV2"
COUNTS = ("MissingRequiredFieldCount", "ProcessingIssueCount", "ConfigRevision")

_ENV = {
    "AWS_REGION": "us-east-1",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - moto dummy credential
    "AWS_SESSION_TOKEN": "testing",  # nosec B105 - moto dummy credential
    "TRACKING_TABLE_NAME": TABLE,
}


def _load_cfn(path):
    class _Loader(yaml.SafeLoader):
        pass

    def _tag(loader, suffix, node):
        if isinstance(node, yaml.ScalarNode):
            return {"!" + suffix: loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {"!" + suffix: loader.construct_sequence(node)}
        return {"!" + suffix: loader.construct_mapping(node)}

    _Loader.add_multi_constructor("!", _tag)
    with open(path, encoding="utf-8") as f:
        return yaml.load(f, Loader=_Loader)  # nosec B506 - SafeLoader subclass


@pytest.fixture(scope="module")
def tracking_table_properties():
    resource = _load_cfn(REPO_ROOT / "template.yaml")["Resources"]["TrackingTable"]
    assert resource["Type"] == "AWS::DynamoDB::Table"
    return resource["Properties"]


@pytest.fixture(scope="module")
def deployed_indexes(tracking_table_properties):
    indexes = {
        g["IndexName"]: g for g in tracking_table_properties["GlobalSecondaryIndexes"]
    }
    assert {OLD_INDEX, NEW_INDEX} <= set(indexes)
    return indexes


def _attributes_the_resolver_reads():
    """Attribute names the resolver can read off an index row.

    Every string constant in the module, whatever reads it (`item.get`,
    `dict.get(item, ...)`, a helper, a renamed variable), so the rule is about
    what the module could name rather than how it spells the read. Over-inclusion
    is harmless: a constant that is not an attribute name matches no projection.
    The call shapes below are collected too, so the non-vacuity check names reads.
    """
    tree = ast.parse(Path(__file__).with_name("index.py").read_text())
    names = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "Attr")
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "item"
                )
            )
        ):
            names.add(node.args[0].value)
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "item"
            and isinstance(node.slice, ast.Constant)
        ):
            names.add(node.slice.value)
    return names


def _load_index():
    spec = importlib.util.spec_from_file_location(
        "list_documents_gsi_list_index", Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["list_documents_gsi_list_index"] = module
    spec.loader.exec_module(module)
    return module


def _create_table(properties, index_names):
    """The deployed TrackingTable's keys, with only the named indexes."""
    dynamodb = boto3.resource("dynamodb")
    gsis = [
        g for g in properties["GlobalSecondaryIndexes"] if g["IndexName"] in index_names
    ]
    assert len(gsis) == len(index_names)
    table = dynamodb.create_table(
        TableName=TABLE,
        KeySchema=properties["KeySchema"],
        AttributeDefinitions=properties["AttributeDefinitions"],
        BillingMode="PAY_PER_REQUEST",
        GlobalSecondaryIndexes=gsis,
    )
    return dynamodb, table


def _put_document(table, n, **attributes):
    table.put_item(
        Item={
            "PK": f"doc#d{n}.pdf",
            "SK": "none",
            "ObjectKey": f"d{n}.pdf",
            "ItemType": "document",
            "InitialEventTime": f"2026-10-01T00:00:{n:02d}Z",
            "ObjectStatus": "COMPLETED",
            # Not projected by either index: proves a row is an index row.
            "Sections": [{"Id": "1"}],
            **attributes,
        }
    )


def _list(module, **arguments):
    return module.handler(
        {
            "info": {"fieldName": "listDocuments"},
            "arguments": arguments,
            "identity": {
                "claims": {"cognito:groups": ["Admin"], "cognito:username": "a"}
            },
        },
        None,
    )


@pytest.fixture
def env():
    with patch.dict(os.environ, _ENV), mock_aws():
        yield


def _resolver(dynamodb):
    module = _load_index()
    module.dynamodb = dynamodb
    return module


class TestTemplate:
    def test_the_list_index_has_the_old_keys(self, deployed_indexes):
        # Same keys means the same rows and an interchangeable nextToken, which is
        # what makes the fallback correct.
        assert (
            deployed_indexes[NEW_INDEX]["KeySchema"]
            == deployed_indexes[OLD_INDEX]["KeySchema"]
        )
        assert deployed_indexes[NEW_INDEX]["Projection"]["ProjectionType"] == "INCLUDE"

    def test_no_index_projects_more_than_dynamodb_accepts(self, deployed_indexes):
        """DynamoDB refuses an INCLUDE projection naming more than 20 attributes.

        Measured on a live stack update, which failed and rolled back with
        "Member must have length less than or equal to 20" for a 23-attribute
        TypeDateIndexV2. Neither moto nor cfn-lint enforces the limit.
        """
        for name, index in deployed_indexes.items():
            assert len(index["Projection"].get("NonKeyAttributes", [])) <= 20, name

    def test_the_list_index_carries_everything_the_list_read_before_plus_the_counts(
        self, deployed_indexes
    ):
        """Derived from the resolver: every attribute it reads off a row, by name."""
        reads = _attributes_the_resolver_reads()
        assert {"ObjectStatus", "HITLReviewOwner", *COUNTS} <= reads  # non-vacuous
        old = set(deployed_indexes[OLD_INDEX]["Projection"]["NonKeyAttributes"])
        new = set(deployed_indexes[NEW_INDEX]["Projection"]["NonKeyAttributes"])
        assert (reads & old) <= new
        assert set(COUNTS) <= new
        assert not set(COUNTS) & old

    def test_the_resolver_names_the_deployed_indexes(self, deployed_indexes):
        module = _load_index()
        assert module.LIST_INDEX == NEW_INDEX
        assert module.TYPE_DATE_INDEX == OLD_INDEX

    def test_the_resolver_may_query_both_indexes(self):
        template = _load_cfn(REPO_ROOT / "nested/api-resolvers/template.yaml")
        function = template["Resources"]["ListDocumentsGSIResolverFunction"]
        resources = [
            r
            for policy in function["Properties"]["Policies"]
            for statement in policy.get("Statement", [])
            if "dynamodb:Query" in statement["Action"]
            for r in statement["Resource"]
        ]
        subs = [r["!Sub"] for r in resources if isinstance(r, dict) and "!Sub" in r]
        for name in (OLD_INDEX, NEW_INDEX):
            assert any(s.endswith(f"/index/{name}") for s in subs), name


class TestListIndex:
    def test_the_list_carries_the_counts(self, env, tracking_table_properties):
        dynamodb, table = _create_table(
            tracking_table_properties, [OLD_INDEX, NEW_INDEX]
        )
        _put_document(
            table,
            1,
            MissingRequiredFieldCount=2,
            ProcessingIssueCount=3,
            ConfigRevision=4,
        )
        _put_document(table, 2, MissingRequiredFieldCount=0, ProcessingIssueCount=0)

        rows = _list(_resolver(dynamodb))["Documents"]

        assert [r["ObjectKey"] for r in rows] == ["d2.pdf", "d1.pdf"]
        assert {k: rows[1][k] for k in COUNTS} == {
            "MissingRequiredFieldCount": 2,
            "ProcessingIssueCount": 3,
            "ConfigRevision": 4,
        }
        # A stored zero is a measurement and survives the None-stripping.
        assert rows[0]["MissingRequiredFieldCount"] == 0
        assert rows[0]["ProcessingIssueCount"] == 0
        assert "Sections" not in rows[0]

    def test_the_count_reads_the_same_rows(self, env, tracking_table_properties):
        dynamodb, table = _create_table(
            tracking_table_properties, [OLD_INDEX, NEW_INDEX]
        )
        for n in range(5):
            _put_document(table, n, ProcessingIssueCount=n)
        module = _resolver(dynamodb)
        count = module.handler(
            {
                "info": {"fieldName": "getDocumentCount"},
                "arguments": {},
                "identity": {"claims": {"cognito:groups": ["Admin"]}},
            },
            None,
        )["count"]
        assert count == len(_list(module)["Documents"]) == 5


class TestFallback:
    @pytest.mark.parametrize(
        "message",
        [
            # Both recorded from live queries against a deployed tracking table.
            # Before the index exists, and while it is CREATING with Backfilling
            # false (resource allocation), and once deleted by hand:
            f"The table does not have the specified index: {NEW_INDEX}",
            # While it is CREATING with Backfilling true:
            f"Cannot read from backfilling global secondary index: {NEW_INDEX}",
        ],
    )
    def test_an_unreadable_index_falls_back(
        self, env, tracking_table_properties, caplog, message
    ):
        """moto answers a query on an absent index with a different error than
        DynamoDB does, so DynamoDB's error is injected rather than taken from moto."""
        dynamodb, table = _create_table(
            tracking_table_properties, [OLD_INDEX, NEW_INDEX]
        )
        _put_document(table, 1, MissingRequiredFieldCount=2, ProcessingIssueCount=3)
        module = _resolver(dynamodb)
        real_query = table.meta.client.query
        calls = []

        def query(**kwargs):
            calls.append(kwargs.get("IndexName"))
            if kwargs.get("IndexName") == NEW_INDEX:
                raise ClientError(
                    {"Error": {"Code": "ValidationException", "Message": message}},
                    "Query",
                )
            return real_query(**kwargs)

        with (
            patch.object(table.meta.client, "query", side_effect=query),
            caplog.at_level("WARNING"),
        ):
            rows = _list(module)["Documents"]

        assert calls == [NEW_INDEX, OLD_INDEX]
        assert [r["ObjectKey"] for r in rows] == ["d1.pdf"]
        # Served by TypeDateIndex, which projects none of the counts.
        assert not set(COUNTS) & set(rows[0])
        assert any(
            NEW_INDEX in r.getMessage() and OLD_INDEX in r.getMessage()
            for r in caplog.records
            if r.levelname == "WARNING"
        )

    def test_a_backfilling_index_falls_back_and_pages_continue_across_indexes(
        self, env, tracking_table_properties
    ):
        dynamodb, table = _create_table(
            tracking_table_properties, [OLD_INDEX, NEW_INDEX]
        )
        for n in range(4):
            _put_document(table, n, ProcessingIssueCount=n)
        module = _resolver(dynamodb)
        real_query = table.meta.client.query
        calls = []
        backfilling = {"on": True}

        def query(**kwargs):
            calls.append(kwargs.get("IndexName"))
            if kwargs.get("IndexName") == NEW_INDEX and backfilling["on"]:
                raise ClientError(
                    {
                        "Error": {
                            "Code": "ValidationException",
                            "Message": (
                                "Cannot read from backfilling global secondary index: "
                                f"{NEW_INDEX}"
                            ),
                        }
                    },
                    "Query",
                )
            return real_query(**kwargs)

        with patch.object(table.meta.client, "query", side_effect=query):
            first = _list(module, limit=2)
            assert calls == [NEW_INDEX, OLD_INDEX]
            assert [r["ObjectKey"] for r in first["Documents"]] == ["d3.pdf", "d2.pdf"]
            assert "ProcessingIssueCount" not in first["Documents"][0]

            # The backfill finishes between two page requests: the token the old
            # index issued continues on the new one, with no row repeated or lost.
            backfilling["on"] = False
            second = _list(module, limit=2, nextToken=first["nextToken"])
            assert calls[-1] == NEW_INDEX
            assert [r["ObjectKey"] for r in second["Documents"]] == ["d1.pdf", "d0.pdf"]
            assert [r["ProcessingIssueCount"] for r in second["Documents"]] == [1, 0]

    @pytest.mark.parametrize(
        "code, message",
        [
            # An invalid request is not an unavailable index: it must surface.
            ("ValidationException", "The provided starting key is invalid"),
            # The same phrase about some other index is not about ours.
            (
                "ValidationException",
                "Cannot read from backfilling global secondary index: OtherIndex",
            ),
            # Only a ValidationException means the index is unreadable, whatever
            # the message says.
            (
                "ResourceNotFoundException",
                f"Cannot read from backfilling global secondary index: {NEW_INDEX}",
            ),
            # A missing grant is a misconfiguration to report, not to route around.
            (
                "AccessDeniedException",
                f"not authorized to perform Query on {NEW_INDEX}",
            ),
        ],
    )
    def test_other_errors_are_not_retried(
        self, env, tracking_table_properties, code, message
    ):
        dynamodb, table = _create_table(
            tracking_table_properties, [OLD_INDEX, NEW_INDEX]
        )
        module = _resolver(dynamodb)
        calls = []

        def query(**kwargs):
            calls.append(kwargs.get("IndexName"))
            raise ClientError({"Error": {"Code": code, "Message": message}}, "Query")

        with patch.object(table.meta.client, "query", side_effect=query):
            with pytest.raises(ClientError):
                _list(module)
        assert calls == [NEW_INDEX]
