import importlib.util
import json
import os
import pathlib
import re
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, patch

import boto3
import pytest
from moto import mock_aws

# Mock environment variables and dependencies before importing
with patch.dict(
    os.environ,
    {
        "TRACKING_TABLE": "test-table",
        "INPUT_BUCKET": "test-bucket",
        "TEST_SET_BUCKET": "test-set-bucket",
        "TEST_SET_COPY_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue",
        "AWS_REGION": "us-east-1",
    },
):
    with patch("idp_common.dynamodb.DynamoDBClient"):
        # Import the specific lambda module
        spec = importlib.util.spec_from_file_location(
            "test_set_index",
            os.path.join(
                os.path.dirname(__file__),
                "../../../../nested/api-resolvers/src/lambda/test_set_resolver/index.py",
            ),
        )
        if spec is None or spec.loader is None:
            raise ImportError("Could not load test_set_resolver module")
        test_set_index = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(test_set_index)


# Test Studio test-set operations are Admin+Author; supply an authorized
# Cognito identity on handler events so the defense-in-depth group gate passes.
_ADMIN_IDENTITY = {
    "claims": {"cognito:groups": ["Admin"], "email": "admin@example.com"}
}


def _extract_set_fields(call_args):
    """Return the set of DDB attribute names that appear in the SET clause of
    an ``update_item`` UpdateExpression.

    Distinguishes actual field writes from names that show up only in the
    ConditionExpression (e.g. ``#st`` for ``#st IN (:completed, :failed)``).
    """
    expr = call_args.kwargs["UpdateExpression"]
    names = call_args.kwargs.get("ExpressionAttributeNames", {})
    set_clause = expr.split(" REMOVE ", 1)[0]
    if not set_clause.startswith("SET "):
        return set()
    set_clause = set_clause[len("SET ") :]
    return {names[alias] for alias in names if f"{alias} = " in set_clause}


def _seed_test_set(table, test_set_id, **extra):
    """Write a minimal, never-published test-set metadata item."""
    item = {"PK": f"testset#{test_set_id}", "SK": "metadata", "id": test_set_id}
    item.update(extra)
    table.put_item(Item=item)


@pytest.fixture
def publish_table():
    """A real (moto) tracking table wired into the resolver's db_client.

    Version allocation depends on DynamoDB's atomic ADD, which a MagicMock
    cannot express — a mocked table would report whatever the test told it to
    and the concurrency guarantee would go untested. The module-level
    db_client is a mock (patched at import), so point its get_item/put_item at
    the real table for the duration of the test.
    """
    # The resolver builds its own boto3 resource with no explicit region, so it
    # picks up the ambient one. Pin the region for both here — other tests in
    # the suite mutate AWS_DEFAULT_REGION, and a mismatch makes the moto table
    # invisible to the resolver (ResourceNotFoundException on UpdateItem).
    region_env = {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        "TRACKING_TABLE": "test-table",
    }
    with mock_aws(), patch.dict(os.environ, region_env):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="test-table",
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

        with _db_client_on(table):
            yield table


def _db_client_on(table):
    """Point the resolver's mocked db_client at a real moto table."""

    def _get_item(key):
        return table.get_item(Key=key).get("Item")

    def _put_item(item, condition_expression=None):
        kwargs = {"Item": item}
        if condition_expression:
            kwargs["ConditionExpression"] = condition_expression
        return table.put_item(**kwargs)

    def _update_item(
        key,
        update_expression,
        expression_attribute_names=None,
        expression_attribute_values=None,
        return_values="ALL_NEW",
    ):
        kwargs = {
            "Key": key,
            "UpdateExpression": update_expression,
            "ReturnValues": return_values,
        }
        if expression_attribute_names:
            kwargs["ExpressionAttributeNames"] = expression_attribute_names
        if expression_attribute_values:
            kwargs["ExpressionAttributeValues"] = expression_attribute_values
        return table.update_item(**kwargs)

    def _delete_item(key):
        return table.delete_item(Key=key)

    def _query(
        key_condition_expression,
        expression_attribute_names=None,
        expression_attribute_values=None,
        limit=None,
        exclusive_start_key=None,
    ):
        kwargs = {"KeyConditionExpression": key_condition_expression}
        if expression_attribute_names:
            kwargs["ExpressionAttributeNames"] = expression_attribute_names
        if expression_attribute_values:
            kwargs["ExpressionAttributeValues"] = expression_attribute_values
        if limit:
            kwargs["Limit"] = limit
        if exclusive_start_key:
            kwargs["ExclusiveStartKey"] = exclusive_start_key
        return table.query(**kwargs)

    def _scan_all(filter_expression=None, expression_attribute_values=None, **_):
        """Emulate DynamoDBClient.scan_all: page moto's Scan and apply the
        filter server-side (moto supports FilterExpression). Only used by the
        get_test_sets fallback when the GSI isn't present on the moto table."""
        kwargs = {}
        if filter_expression:
            kwargs["FilterExpression"] = filter_expression
        if expression_attribute_values:
            kwargs["ExpressionAttributeValues"] = expression_attribute_values
        items = []
        last = None
        while True:
            if last is not None:
                kwargs["ExclusiveStartKey"] = last
            resp = table.scan(**kwargs)
            items.extend(resp.get("Items", []))
            last = resp.get("LastEvaluatedKey")
            if not last:
                break
        return items

    return _MultiPatch(
        patch.object(test_set_index.db_client, "get_item", side_effect=_get_item),
        patch.object(test_set_index.db_client, "put_item", side_effect=_put_item),
        patch.object(test_set_index.db_client, "update_item", side_effect=_update_item),
        patch.object(test_set_index.db_client, "delete_item", side_effect=_delete_item),
        patch.object(test_set_index.db_client, "query", side_effect=_query),
        patch.object(test_set_index.db_client, "scan_all", side_effect=_scan_all),
    )


class _MultiPatch:
    """Enter/exit several patches as one context manager."""

    def __init__(self, *patches):
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


@pytest.fixture
def labeling_env():
    """Real (moto) DynamoDB + S3 for the draft-labeling primitive.

    The harvester's whole job is moving JSON between real S3 keys and deciding
    what to overwrite, so mocked S3 clients would assert on call shapes instead
    of the actual outcome. Yields (table, s3_client).
    """
    region_env = {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        "TRACKING_TABLE": "test-table",
        "TEST_SET_BUCKET": "test-set-bucket",
        "TEST_RUNNER_FUNCTION_ARN": "arn:aws:lambda:us-east-1:123456789012:function:runner",
    }
    with mock_aws(), patch.dict(os.environ, region_env):
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        table = ddb.create_table(
            TableName="test-table",
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
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-set-bucket")
        s3.create_bucket(Bucket="output-bucket")

        with _db_client_on(table), patch.object(test_set_index, "s3_client", s3):
            yield table, s3


def _seed_pipeline_result(s3, key, inference, explainability=None):
    """Write a pipeline section extraction result to the output bucket."""
    body = {"inference_result": inference}
    if explainability is not None:
        body["explainability_info"] = explainability
    s3.put_object(
        Bucket="output-bucket", Key=key, Body=json.dumps(body).encode("utf-8")
    )
    return f"s3://output-bucket/{key}"


def _seed_completed_run(table, job_id, test_set_id, files, sections_by_file):
    """Write a finished test run plus its per-document items."""
    table.put_item(
        Item={
            "PK": f"testrun#{job_id}",
            "SK": "metadata",
            "TestSetId": test_set_id,
            "Files": files,
            "Status": "RUNNING",
        }
    )
    for file_name in files:
        table.put_item(
            Item={
                "PK": f"doc#{job_id}/{file_name}",
                "SK": "none",
                "ObjectStatus": "COMPLETED",
                "Sections": sections_by_file.get(file_name, []),
            }
        )


@pytest.fixture(autouse=True)
def _reset_reconcile_module_state():
    """Reset the reconcile helper's module-level singletons between tests.

    ``_RECONCILE_MEMO`` (warm-container per-prefix cache) and
    ``_tracking_table`` (boto3.resource singleton) would otherwise leak
    state across tests and cause ordering-dependent failures.
    """
    test_set_index._RECONCILE_MEMO.clear()
    test_set_index._tracking_table = None
    yield
    test_set_index._RECONCILE_MEMO.clear()
    test_set_index._tracking_table = None


@pytest.mark.unit
class TestTestSetResolver:
    def test_handler_field_routing(self):
        """Test that handler routes to correct functions"""
        with patch.object(test_set_index, "add_test_set") as mock_add:
            mock_add.return_value = {"id": "test"}
            event = {
                "info": {"fieldName": "addTestSet"},
                "arguments": {},
                "identity": _ADMIN_IDENTITY,
            }
            test_set_index.handler(event, {})
            mock_add.assert_called_once()

        with patch.object(test_set_index, "get_test_sets") as mock_get:
            mock_get.return_value = []
            event = {"info": {"fieldName": "getTestSets"}, "identity": _ADMIN_IDENTITY}
            test_set_index.handler(event, {})
            mock_get.assert_called_once()

        with patch.object(test_set_index, "update_test_set") as mock_update:
            mock_update.return_value = {"id": "test"}
            event = {
                "info": {"fieldName": "updateTestSet"},
                "arguments": {},
                "identity": _ADMIN_IDENTITY,
            }
            test_set_index.handler(event, {})
            mock_update.assert_called_once()

    def test_handler_unknown_field(self):
        """Test handler with unknown field"""
        event = {
            "info": {"fieldName": "unknown"},
            "arguments": {},
            "identity": _ADMIN_IDENTITY,
        }
        with pytest.raises(Exception, match="Unknown field: unknown"):
            test_set_index.handler(event, {})

    def test_handler_rejects_viewer(self):
        """Defense-in-depth: a Viewer must not reach any test-set operation."""
        event = {
            "info": {"fieldName": "addTestSet"},
            "arguments": {},
            "identity": {"claims": {"cognito:groups": ["Viewer"]}},
        }
        with pytest.raises(Exception, match="requires Admin or Author group"):
            test_set_index.handler(event, {})

    def test_handler_allows_direct_lambda_invoke_no_identity(self):
        """RBAC bypass: direct Lambda invocation (no identity) proceeds for CI/automation."""
        with patch.object(test_set_index, "get_test_sets") as mock_get:
            mock_get.return_value = []
            # Direct Lambda invoke: no 'identity' field (CI/automation path)
            event = {"info": {"fieldName": "getTestSets"}}
            # Should NOT raise - bypass works as designed
            test_set_index.handler(event, {})
            mock_get.assert_called_once()

    def test_handler_allows_direct_lambda_invoke_identity_none(self):
        """RBAC bypass: direct Lambda invocation (identity=None) proceeds for CI/automation."""
        with patch.object(test_set_index, "get_test_sets") as mock_get:
            mock_get.return_value = []
            # Direct Lambda invoke: identity explicitly None
            event = {"info": {"fieldName": "getTestSets"}, "identity": None}
            # Should NOT raise - bypass works as designed
            test_set_index.handler(event, {})
            mock_get.assert_called_once()

    def test_handler_still_enforces_rbac_for_appsync_viewer(self):
        """Regression guard: AppSync invocation with non-Admin/Author still raises."""
        # This is the same as test_handler_rejects_viewer but explicitly tests
        # that the RBAC bypass doesn't break AppSync RBAC enforcement
        event = {
            "info": {"fieldName": "getTestSets"},
            "identity": {"claims": {"cognito:groups": ["Viewer"]}},
        }
        with pytest.raises(Exception, match="requires Admin or Author group"):
            test_set_index.handler(event, {})

    @patch("uuid.uuid4")
    @patch("datetime.datetime")
    @patch("boto3.client")
    @patch.dict(
        os.environ,
        {
            "TEST_SET_COPY_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123456789012/test-queue",
            "TRACKING_TABLE": "test-table",
            "TEST_SET_BUCKET": "test-set-bucket",
        },
    )
    def test_add_test_set_structure(self, mock_boto3, mock_datetime, mock_uuid):
        """Test add_test_set returns correct structure"""
        mock_uuid.return_value = "test-id"
        mock_datetime.utcnow.return_value.isoformat.return_value = "2025-10-17T16:00:00"

        # Mock SQS client
        mock_sqs = Mock()
        mock_boto3.return_value = mock_sqs

        with patch.object(test_set_index.db_client, "put_item") as mock_put:
            args = {
                "name": "test",
                "filePattern": "*.pdf",
                "fileCount": 5,
                "bucketType": "input",
            }
            result = test_set_index.add_test_set(args)

            mock_put.assert_called_once()
            assert result["id"] == "test"  # ID is generated from name
            assert result["name"] == "test"
            assert result["name"] == "test"
            assert result["filePattern"] == "*.pdf"
            assert result["fileCount"] == 5
            assert "createdAt" in result

    @patch.dict(os.environ, {"TEST_SET_BUCKET": "test-set-bucket"})
    def test_delete_test_sets_calls_client(self):
        """Test delete_test_sets uses DynamoDB client"""
        with patch.object(test_set_index.db_client, "delete_item") as mock_delete:
            args = {"testSetIds": ["id1", "id2"]}
            result = test_set_index.delete_test_sets(args)

            assert mock_delete.call_count == 2
            assert result is True

    @patch.dict(os.environ, {"TEST_SET_BUCKET": "test-set-bucket"})
    def test_delete_test_sets_paginates_beyond_1000_objects(self):
        """Every object is deleted, not just the first list page.

        Regression: both S3 APIs involved are page-limited at 1000, so an
        unpaginated pass orphans everything past the first page — the test set
        disappears while its files stay in the bucket. Real test sets exceed 1000
        objects easily.
        """
        total_objects = 2500
        keys = [f"big-set/input/doc{i}.pdf" for i in range(total_objects)]

        def fake_list(**kwargs):
            start = int(kwargs.get("ContinuationToken") or 0)
            page = keys[start : start + 1000]
            nxt = start + 1000
            truncated = nxt < len(keys)
            resp = {
                "Contents": [{"Key": k} for k in page],
                "IsTruncated": truncated,
            }
            if truncated:
                resp["NextContinuationToken"] = str(nxt)
            return resp

        deleted = []

        def fake_delete_objects(**kwargs):
            batch = kwargs["Delete"]["Objects"]
            # S3 rejects a batch larger than 1000.
            assert len(batch) <= 1000
            deleted.extend(o["Key"] for o in batch)
            return {}

        with patch.object(test_set_index.db_client, "delete_item"):
            with patch.object(
                test_set_index.s3_client, "list_objects_v2", side_effect=fake_list
            ):
                with patch.object(
                    test_set_index.s3_client,
                    "delete_objects",
                    side_effect=fake_delete_objects,
                ):
                    result = test_set_index.delete_test_sets(
                        {"testSetIds": ["big-set"]}
                    )

        assert result is True
        assert sorted(deleted) == sorted(keys), (
            f"expected all {total_objects} objects deleted, got {len(deleted)}"
        )

    @patch.dict(os.environ, {"TEST_SET_BUCKET": "test-set-bucket"})
    def test_delete_test_sets_stops_on_truncated_page_without_token(self):
        """A truncated response with no continuation token must not loop forever."""
        responses = [
            {
                "Contents": [{"Key": "s/input/a.pdf"}],
                "IsTruncated": True,
                # No NextContinuationToken — malformed/edge response.
            }
        ]

        with patch.object(test_set_index.db_client, "delete_item"):
            with patch.object(
                test_set_index.s3_client, "list_objects_v2", side_effect=responses * 5
            ):
                with patch.object(test_set_index.s3_client, "delete_objects"):
                    result = test_set_index.delete_test_sets({"testSetIds": ["s"]})

        assert result is True

    @patch.dict(
        os.environ, {"INPUT_BUCKET": "test-bucket", "TRACKING_TABLE": "test-table"}
    )
    def test_get_test_sets_uses_gsi_and_batch(self):
        """Test get_test_sets uses GSI query + BatchGetItem"""
        with patch.object(test_set_index, "find_matching_files") as mock_find_files:
            mock_find_files.return_value = ["file1.pdf", "file2.pdf", "file3.pdf"]

            with patch.object(test_set_index, "boto3") as mock_boto3:
                # Mock GSI query returning keys
                mock_table = MagicMock()
                mock_table.query.return_value = {
                    "Items": [{"PK": "testset#test-id", "SK": "metadata"}]
                }
                mock_boto3.resource.return_value.Table.return_value = mock_table

                # Mock BatchGetItem returning full records
                mock_boto3.resource.return_value.batch_get_item.return_value = {
                    "Responses": {
                        "test-table": [
                            {
                                "PK": "testset#test-id",
                                "SK": "metadata",
                                "id": "test-id",
                                "name": "test-name",
                                "filePattern": "*.pdf",
                                "fileCount": 5,
                                "createdAt": "2025-10-17T16:00:00Z",
                            }
                        ]
                    }
                }

                result = test_set_index.get_test_sets()

                mock_table.query.assert_called_once()
                assert len(result) == 1
                assert result[0]["id"] == "test-id"
                # 'source' maps through; absent on this record -> None (back-compat)
                assert result[0]["source"] is None
                # Absent on the record -> None, not a KeyError. Stack-managed
                # benchmark sets don't set it; the UI falls back to matching a
                # config version named after the test set id.
                assert result[0]["configVersion"] is None

    @patch.dict(
        os.environ, {"INPUT_BUCKET": "test-bucket", "TRACKING_TABLE": "test-table"}
    )
    def test_get_test_sets_maps_source_when_present(self):
        """A record's 'source' attribute is returned in the mapped result."""
        with patch.object(test_set_index, "find_matching_files") as mock_find_files:
            mock_find_files.return_value = []
            with patch.object(test_set_index, "boto3") as mock_boto3:
                mock_table = MagicMock()
                mock_table.query.return_value = {
                    "Items": [{"PK": "testset#syn-id", "SK": "metadata"}]
                }
                mock_boto3.resource.return_value.Table.return_value = mock_table
                mock_boto3.resource.return_value.batch_get_item.return_value = {
                    "Responses": {
                        "test-table": [
                            {
                                "PK": "testset#syn-id",
                                "SK": "metadata",
                                "id": "syn-id",
                                "name": "syn-name",
                                "source": "synthetic",
                                "createdAt": "2025-10-17T16:00:00Z",
                            }
                        ]
                    }
                }

                result = test_set_index.get_test_sets()
                assert result[0]["source"] == "synthetic"

    @patch.dict(
        os.environ, {"TRACKING_TABLE": "test-table", "INPUT_BUCKET": "test-bucket"}
    )
    def test_get_test_sets_passes_through_declared_config_version(self):
        """A test set may DECLARE the config version Test Studio preselects.

        Needed by extension-deployed test sets: the Feature Platform names their
        config presets `<featureId>-v<version>`, which can never equal the test
        set id, so the id-matching convention cannot reach them.
        """
        with patch.object(test_set_index, "find_matching_files") as mock_find_files:
            mock_find_files.return_value = []

            with patch.object(test_set_index, "boto3") as mock_boto3:
                mock_table = MagicMock()
                mock_table.query.return_value = {
                    "Items": [{"PK": "testset#confbench-clean", "SK": "metadata"}]
                }
                mock_boto3.resource.return_value.Table.return_value = mock_table
                mock_boto3.resource.return_value.batch_get_item.return_value = {
                    "Responses": {
                        "test-table": [
                            {
                                "PK": "testset#confbench-clean",
                                "SK": "metadata",
                                "id": "confbench-clean",
                                "name": "ConfBench (clean baseline)",
                                "fileCount": 75,
                                "createdAt": "2026-08-05T16:00:00Z",
                                "configVersion": "confbench-testset-v0.1.0",
                            }
                        ]
                    }
                }

                result = test_set_index.get_test_sets()

                assert len(result) == 1
                assert result[0]["configVersion"] == "confbench-testset-v0.1.0"

    def test_get_test_set_source_reads_marker(self):
        """_get_test_set_source distinguishes 404 (uploaded) from transient errors.

        A 404 is a definitive answer (no marker there); a throttling or 5xx is
        not, and returning 'uploaded' on those would silently misclassify
        synthetic sets. The helper now returns None on non-404 errors so
        reconcile can leave the row's existing source alone.
        """
        from botocore.exceptions import ClientError

        s3 = MagicMock()
        # marker present -> synthetic
        s3.head_object.return_value = {}
        assert (
            test_set_index._get_test_set_source(s3, "bucket", "prefix") == "synthetic"
        )
        # definitive 404 -> uploaded
        s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )
        assert test_set_index._get_test_set_source(s3, "bucket", "prefix") == "uploaded"
        # transient error (e.g. SlowDown) -> None (unknown, don't overwrite)
        s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "SlowDown", "Message": "throttled"}}, "HeadObject"
        )
        assert test_set_index._get_test_set_source(s3, "bucket", "prefix") is None

    @patch.dict("os.environ", {"INPUT_BUCKET": "test-bucket"})
    def test_list_input_bucket_files(self):
        """Test list_input_bucket_files calls find_matching_files"""
        with patch.object(test_set_index, "find_matching_files") as mock_find:
            mock_find.return_value = ["file1.pdf", "file2.pdf"]

            args = {"filePattern": "*.pdf", "bucketType": "input"}
            result = test_set_index.list_bucket_files(args)

            mock_find.assert_called_once_with(
                "test-bucket", "*.pdf", modified_after=None
            )
            assert result == ["file1.pdf", "file2.pdf"]

    @patch.dict(
        os.environ,
        {"TRACKING_TABLE": "test-table", "TEST_SET_BUCKET": "test-set-bucket"},
    )
    def test_update_test_set_description_only(self):
        """Test updating test set description only"""
        with patch.object(test_set_index.db_client, "get_item") as mock_get:
            mock_get.return_value = {
                "PK": "testset#test-id",
                "SK": "metadata",
                "id": "test-id",
                "name": "test-set",
                "description": "old description",
                "filePattern": "*.pdf",
                "fileCount": 5,
                "createdAt": "2025-10-17T16:00:00Z",
                "documentClassType": "SINGLE_CLASS",
            }

            with patch.object(test_set_index, "boto3") as mock_boto3:
                mock_table = MagicMock()
                mock_table.update_item.return_value = {
                    "Attributes": {
                        "id": "test-id",
                        "name": "test-set",
                        "description": "new description",
                        "filePattern": "*.pdf",
                        "fileCount": 5,
                        "createdAt": "2025-10-17T16:00:00Z",
                        "documentClassType": "SINGLE_CLASS",
                    }
                }
                mock_boto3.resource.return_value.Table.return_value = mock_table

                args = {"input": {"id": "test-id", "description": "new description"}}
                result = test_set_index.update_test_set(args)

                # Verify update was called with correct expression
                mock_table.update_item.assert_called_once()
                call_args = mock_table.update_item.call_args
                assert "SET #desc = :desc" in call_args[1]["UpdateExpression"]
                assert (
                    call_args[1]["ExpressionAttributeValues"][":desc"]
                    == "new description"
                )
                assert (
                    call_args[1]["ExpressionAttributeNames"]["#desc"] == "description"
                )

                # Verify result
                assert result["id"] == "test-id"
                assert result["description"] == "new description"
                assert result["documentClassType"] == "SINGLE_CLASS"

    @patch.dict(
        os.environ,
        {"TRACKING_TABLE": "test-table", "TEST_SET_BUCKET": "test-set-bucket"},
    )
    def test_update_test_set_document_class_type_only(self):
        """Test updating test set documentClassType only"""
        with patch.object(test_set_index.db_client, "get_item") as mock_get:
            mock_get.return_value = {
                "PK": "testset#test-id",
                "SK": "metadata",
                "id": "test-id",
                "name": "test-set",
                "description": "test description",
                "filePattern": "*.pdf",
                "fileCount": 5,
                "createdAt": "2025-10-17T16:00:00Z",
            }

            with patch.object(test_set_index, "boto3") as mock_boto3:
                mock_table = MagicMock()
                mock_table.update_item.return_value = {
                    "Attributes": {
                        "id": "test-id",
                        "name": "test-set",
                        "description": "test description",
                        "filePattern": "*.pdf",
                        "fileCount": 5,
                        "createdAt": "2025-10-17T16:00:00Z",
                        "documentClassType": "MULTI_CLASS",
                    }
                }
                mock_boto3.resource.return_value.Table.return_value = mock_table

                args = {"input": {"id": "test-id", "documentClassType": "MULTI_CLASS"}}
                result = test_set_index.update_test_set(args)

                # Verify update was called with correct expression
                mock_table.update_item.assert_called_once()
                call_args = mock_table.update_item.call_args
                assert (
                    "SET documentClassType = :docType"
                    in call_args[1]["UpdateExpression"]
                )
                assert (
                    call_args[1]["ExpressionAttributeValues"][":docType"]
                    == "MULTI_CLASS"
                )

                # Verify result
                assert result["id"] == "test-id"
                assert result["documentClassType"] == "MULTI_CLASS"

    @patch.dict(
        os.environ,
        {"TRACKING_TABLE": "test-table", "TEST_SET_BUCKET": "test-set-bucket"},
    )
    def test_update_test_set_remove_document_class_type(self):
        """Test removing documentClassType by setting to None"""
        with patch.object(test_set_index.db_client, "get_item") as mock_get:
            mock_get.return_value = {
                "PK": "testset#test-id",
                "SK": "metadata",
                "id": "test-id",
                "name": "test-set",
                "description": "test description",
                "filePattern": "*.pdf",
                "fileCount": 5,
                "createdAt": "2025-10-17T16:00:00Z",
                "documentClassType": "SINGLE_CLASS",
            }

            with patch.object(test_set_index, "boto3") as mock_boto3:
                mock_table = MagicMock()
                mock_table.update_item.return_value = {
                    "Attributes": {
                        "id": "test-id",
                        "name": "test-set",
                        "description": "test description",
                        "filePattern": "*.pdf",
                        "fileCount": 5,
                        "createdAt": "2025-10-17T16:00:00Z",
                    }
                }
                mock_boto3.resource.return_value.Table.return_value = mock_table

                args = {"input": {"id": "test-id", "documentClassType": None}}
                result = test_set_index.update_test_set(args)

                # Verify update was called with REMOVE expression
                mock_table.update_item.assert_called_once()
                call_args = mock_table.update_item.call_args
                assert "REMOVE documentClassType" in call_args[1]["UpdateExpression"]
                # Should not have :docType in expression values when removing
                assert ":docType" not in call_args[1].get(
                    "ExpressionAttributeValues", {}
                )

                # Verify result has documentClassType as None (removed from DynamoDB)
                assert result["id"] == "test-id"
                assert result["documentClassType"] is None

    @patch.dict(
        os.environ,
        {"TRACKING_TABLE": "test-table", "TEST_SET_BUCKET": "test-set-bucket"},
    )
    def test_update_test_set_both_fields(self):
        """Test updating both description and documentClassType"""
        with patch.object(test_set_index.db_client, "get_item") as mock_get:
            mock_get.return_value = {
                "PK": "testset#test-id",
                "SK": "metadata",
                "id": "test-id",
                "name": "test-set",
                "description": "old description",
                "filePattern": "*.pdf",
                "fileCount": 5,
                "createdAt": "2025-10-17T16:00:00Z",
                "documentClassType": "SINGLE_CLASS",
            }

            with patch.object(test_set_index, "boto3") as mock_boto3:
                mock_table = MagicMock()
                mock_table.update_item.return_value = {
                    "Attributes": {
                        "id": "test-id",
                        "name": "test-set",
                        "description": "new description",
                        "filePattern": "*.pdf",
                        "fileCount": 5,
                        "createdAt": "2025-10-17T16:00:00Z",
                        "documentClassType": "PACKET_SPLITTING",
                    }
                }
                mock_boto3.resource.return_value.Table.return_value = mock_table

                args = {
                    "input": {
                        "id": "test-id",
                        "description": "new description",
                        "documentClassType": "PACKET_SPLITTING",
                    }
                }
                result = test_set_index.update_test_set(args)

                # Verify update was called with both fields in SET clause
                mock_table.update_item.assert_called_once()
                call_args = mock_table.update_item.call_args
                update_expr = call_args[1]["UpdateExpression"]
                assert "SET" in update_expr
                assert "#desc = :desc" in update_expr
                assert "documentClassType = :docType" in update_expr
                assert (
                    call_args[1]["ExpressionAttributeValues"][":desc"]
                    == "new description"
                )
                assert (
                    call_args[1]["ExpressionAttributeValues"][":docType"]
                    == "PACKET_SPLITTING"
                )

                # Verify result
                assert result["id"] == "test-id"
                assert result["description"] == "new description"
                assert result["documentClassType"] == "PACKET_SPLITTING"

    @patch.dict(
        os.environ,
        {"TRACKING_TABLE": "test-table", "TEST_SET_BUCKET": "test-set-bucket"},
    )
    def test_update_test_set_description_and_remove_document_class_type(self):
        """Test updating description while removing documentClassType"""
        with patch.object(test_set_index.db_client, "get_item") as mock_get:
            mock_get.return_value = {
                "PK": "testset#test-id",
                "SK": "metadata",
                "id": "test-id",
                "name": "test-set",
                "description": "old description",
                "filePattern": "*.pdf",
                "fileCount": 5,
                "createdAt": "2025-10-17T16:00:00Z",
                "documentClassType": "SINGLE_CLASS",
            }

            with patch.object(test_set_index, "boto3") as mock_boto3:
                mock_table = MagicMock()
                mock_table.update_item.return_value = {
                    "Attributes": {
                        "id": "test-id",
                        "name": "test-set",
                        "description": "new description",
                        "filePattern": "*.pdf",
                        "fileCount": 5,
                        "createdAt": "2025-10-17T16:00:00Z",
                    }
                }
                mock_boto3.resource.return_value.Table.return_value = mock_table

                args = {
                    "input": {
                        "id": "test-id",
                        "description": "new description",
                        "documentClassType": None,
                    }
                }
                result = test_set_index.update_test_set(args)

                # Verify update was called with SET and REMOVE
                mock_table.update_item.assert_called_once()
                call_args = mock_table.update_item.call_args
                update_expr = call_args[1]["UpdateExpression"]
                assert "SET #desc = :desc" in update_expr
                assert "REMOVE documentClassType" in update_expr
                assert (
                    call_args[1]["ExpressionAttributeValues"][":desc"]
                    == "new description"
                )

                # Verify result has documentClassType as None (removed from DynamoDB)
                assert result["id"] == "test-id"
                assert result["description"] == "new description"
                assert result["documentClassType"] is None

    @patch.dict(
        os.environ,
        {"TRACKING_TABLE": "test-table", "TEST_SET_BUCKET": "test-set-bucket"},
    )
    def test_update_test_set_no_changes(self):
        """Test update_test_set with no actual changes"""
        with patch.object(test_set_index.db_client, "get_item") as mock_get:
            mock_get.return_value = {
                "PK": "testset#test-id",
                "SK": "metadata",
                "id": "test-id",
                "name": "test-set",
                "description": "test description",
                "filePattern": "*.pdf",
                "fileCount": 5,
                "createdAt": "2025-10-17T16:00:00Z",
            }

            with patch.object(test_set_index.db_client, "update_item") as mock_update:
                args = {"input": {"id": "test-id"}}
                result = test_set_index.update_test_set(args)

                # Should not call update_item when there are no changes
                mock_update.assert_not_called()

                # Should return the current item
                assert result["id"] == "test-id"
                assert result["description"] == "test description"

    @patch.dict(
        os.environ,
        {"TRACKING_TABLE": "test-table", "TEST_SET_BUCKET": "test-set-bucket"},
    )
    def test_update_test_set_invalid_description(self):
        """Test update_test_set with invalid description length"""
        args = {"input": {"id": "test-id", "description": "x" * 501}}

        with pytest.raises(Exception, match="Description cannot exceed 500 characters"):
            test_set_index.update_test_set(args)

    @patch.dict(
        os.environ,
        {"TRACKING_TABLE": "test-table", "TEST_SET_BUCKET": "test-set-bucket"},
    )
    def test_update_test_set_nonexistent_id(self):
        """Test update_test_set with non-existent test set ID"""
        with patch.object(test_set_index.db_client, "get_item") as mock_get:
            mock_get.return_value = None

            args = {"input": {"id": "nonexistent-id", "description": "new description"}}

            with pytest.raises(Exception, match="Test set 'nonexistent-id' not found"):
                test_set_index.update_test_set(args)

    # -- Versioning -------------------------------------------------------

    def test_publish_first_version_sets_active_reference(self, publish_table):
        """Publishing with no prior versions creates v1 and makes it active."""
        _seed_test_set(publish_table, "ts1", source="uploaded", fileCount=10)

        result = test_set_index.publish_test_set_version(
            {"input": {"testSetId": "ts1", "label": "first"}}
        )

        assert result["version"] == 1
        assert result["label"] == "first"
        assert result["activeReference"] == 1
        # An immutable version item was written under SK=version#000001
        written = publish_table.get_item(
            Key={"PK": "testset#ts1", "SK": "version#000001"}
        )["Item"]
        assert written["ItemType"] == "testset_version"
        assert written["versionNumber"] == 1
        # The metadata pointers were advanced and the active reference set
        meta = publish_table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})[
            "Item"
        ]
        assert meta["latestVersion"] == 1
        assert meta["publishedVersion"] == 1
        assert meta["activeReference"] == 1

    def test_publish_increments_and_can_skip_active(self, publish_table):
        """Second publish is v2; setAsActiveReference=false leaves active alone."""
        _seed_test_set(publish_table, "ts1")
        test_set_index.publish_test_set_version({"input": {"testSetId": "ts1"}})

        result = test_set_index.publish_test_set_version(
            {"input": {"testSetId": "ts1", "setAsActiveReference": False}}
        )

        assert result["version"] == 2
        # active reference unchanged (still 1)
        assert result["activeReference"] == 1
        meta = publish_table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})[
            "Item"
        ]
        assert meta["latestVersion"] == 2
        assert meta["publishedVersion"] == 2
        assert meta["activeReference"] == 1

    def test_concurrent_publishes_get_distinct_versions(self, publish_table):
        """Two interleaved publishes must not collide on one version number.

        Regression: allocating from a previously-read latestVersion is a
        read-modify-write race that lets both callers write version#000001. The
        number is reserved by an atomic ADD, so interleaved reads still yield
        distinct versions and two surviving items.
        """
        _seed_test_set(publish_table, "ts1")

        real_get_item = test_set_index.db_client.get_item
        second_result = {}
        reentered = []

        def publish_other_first(key):
            """On the first caller's metadata read, run a whole second publish.

            This forces the worst-case interleaving — the first caller now
            holds a metadata snapshot taken before the second publish landed.
            The reentry flag is set *before* recursing so the nested publish's
            own metadata read doesn't trigger another one.
            """
            meta = real_get_item(key)
            if not reentered:
                reentered.append(True)
                second_result["r"] = test_set_index.publish_test_set_version(
                    {"input": {"testSetId": "ts1"}}
                )
            return meta

        with patch.object(
            test_set_index.db_client, "get_item", side_effect=publish_other_first
        ):
            first_result = test_set_index.publish_test_set_version(
                {"input": {"testSetId": "ts1"}}
            )

        versions = {second_result["r"]["version"], first_result["version"]}
        assert versions == {1, 2}, f"expected distinct versions, got {versions}"
        # Both immutable items survive — neither overwrote the other.
        stored = test_set_index.get_test_set_versions({"testSetId": "ts1"})
        assert [v["version"] for v in stored] == [1, 2]

    def test_publish_pointers_only_move_forward(self, publish_table):
        """An out-of-order publish must not rewind publishedVersion.

        Concurrent publishes can reach the pointer write in either order; the
        older version landing second must leave the pointers on the newer one.
        Seeding pointers ahead of the counter reproduces that end state: the
        reservation hands out v1 while the pointers already say v5.
        """
        _seed_test_set(
            publish_table, "ts1", publishedVersion=5, activeReference=5, latestVersion=0
        )

        result = test_set_index.publish_test_set_version(
            {"input": {"testSetId": "ts1"}}
        )

        # The version item is still written — this caller's work is not lost.
        assert result["version"] == 1
        assert (
            publish_table.get_item(Key={"PK": "testset#ts1", "SK": "version#000001"})[
                "Item"
            ]["versionNumber"]
            == 1
        )
        # But the pointers were NOT rewound to 1.
        meta = publish_table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})[
            "Item"
        ]
        assert meta["publishedVersion"] == 5
        assert meta["activeReference"] == 5

    def test_publish_nonexistent_test_set_raises(self, publish_table):
        with pytest.raises(Exception, match="Test set 'ghost' not found"):
            test_set_index.publish_test_set_version({"input": {"testSetId": "ghost"}})

    def test_publish_race_on_deleted_test_set_raises(self, publish_table):
        """A set deleted between the metadata read and the reservation must not
        be resurrected by update_item's upsert semantics."""
        _seed_test_set(publish_table, "ts1")
        real_get_item = test_set_index.db_client.get_item

        def delete_after_read(key):
            meta = real_get_item(key)
            publish_table.delete_item(Key={"PK": "testset#ts1", "SK": "metadata"})
            return meta

        with patch.object(
            test_set_index.db_client, "get_item", side_effect=delete_after_read
        ):
            with pytest.raises(Exception, match="Test set 'ts1' not found"):
                test_set_index.publish_test_set_version({"input": {"testSetId": "ts1"}})

        assert "Item" not in publish_table.get_item(
            Key={"PK": "testset#ts1", "SK": "metadata"}
        )

    @patch.dict(os.environ, {"TRACKING_TABLE": "test-table"})
    def test_get_test_set_versions_maps_and_sorts(self):
        with patch.object(test_set_index, "boto3") as mock_boto3:
            mock_table = MagicMock()
            mock_table.query.return_value = {
                "Items": [
                    {
                        "testSetId": "ts1",
                        "versionNumber": 2,
                        "label": "v2",
                        "fileCount": 12,
                        "createdAt": "2026-01-02T00:00:00Z",
                    },
                    {
                        "testSetId": "ts1",
                        "versionNumber": 1,
                        "label": "v1",
                        "fileCount": 10,
                        "createdAt": "2026-01-01T00:00:00Z",
                    },
                ]
            }
            mock_boto3.resource.return_value.Table.return_value = mock_table

            result = test_set_index.get_test_set_versions({"testSetId": "ts1"})

            assert [r["version"] for r in result] == [1, 2]  # ascending
            assert result[0]["label"] == "v1"
            assert result[1]["fileCount"] == 12

    # -- Membership editing: remove ---------------------------------------

    @patch.dict(
        os.environ, {"TRACKING_TABLE": "test-table", "TEST_SET_BUCKET": "ts-bucket"}
    )
    def test_remove_documents_deletes_input_and_baseline_and_recounts(self):
        # Patches the module-level client, which is the one configured for the
        # private-VPC endpoint; a locally-constructed client would bypass it.
        s3 = MagicMock()
        with (
            patch.object(test_set_index.db_client, "get_item") as mock_get,
            patch.object(test_set_index, "boto3") as mock_boto3,
            patch.object(test_set_index, "s3_client", s3),
            patch.object(test_set_index, "_validate_test_set_files") as mock_validate,
        ):
            mock_get.return_value = {
                "id": "ts1",
                "name": "TS One",
                "status": "COMPLETED",
                "createdAt": "2026-01-01T00:00:00Z",
            }
            # baseline folder for doc.pdf has one nested result.json
            paginator = MagicMock()
            paginator.paginate.return_value = [
                {"Contents": [{"Key": "ts1/baseline/doc.pdf/sections/1/result.json"}]}
            ]
            s3.get_paginator.return_value = paginator
            mock_table = MagicMock()

            def _resource(name):
                return MagicMock(Table=MagicMock(return_value=mock_table))

            mock_boto3.client.return_value = s3
            mock_boto3.resource.side_effect = _resource
            mock_validate.return_value = {"valid": True, "input_count": 4}

            result = test_set_index.remove_documents_from_test_set(
                {"testSetId": "ts1", "fileNames": ["doc.pdf"]}
            )

            # Deleted both the input object and the baseline result
            deleted_keys = set()
            for call in s3.delete_objects.call_args_list:
                for obj in call.kwargs["Delete"]["Objects"]:
                    deleted_keys.add(obj["Key"])
            assert "ts1/input/doc.pdf" in deleted_keys
            assert "ts1/baseline/doc.pdf/sections/1/result.json" in deleted_keys
            # fileCount updated to the recounted value
            assert result["fileCount"] == 4
            update_kwargs = mock_table.update_item.call_args.kwargs
            assert update_kwargs["ExpressionAttributeValues"][":c"] == 4

    def test_remove_documents_nonexistent_test_set_raises(self):
        with patch.object(test_set_index.db_client, "get_item") as mock_get:
            mock_get.return_value = None
            with pytest.raises(Exception, match="Test set 'ghost' not found"):
                test_set_index.remove_documents_from_test_set(
                    {"testSetId": "ghost", "fileNames": ["a.pdf"]}
                )

    # -- Unlabeled sets (the draft-labeling on-ramp) -----------------------

    def test_validation_allows_a_set_with_no_baseline_when_opted_in(self):
        """'Upload documents only' is a valid set awaiting draft labels."""
        s3 = Mock()
        s3.get_paginator.return_value.paginate.side_effect = lambda **kw: (
            [{"Contents": [{"Key": "ts1/input/a.pdf"}, {"Key": "ts1/input/b.pdf"}]}]
            if "input" in kw["Prefix"]
            else [{}]
        )

        strict = test_set_index._validate_test_set_files(s3, "bucket", "ts1")
        assert strict["valid"] is False
        assert strict["error"] == "No baseline files found"

        relaxed = test_set_index._validate_test_set_files(
            s3, "bucket", "ts1", allow_unlabeled=True
        )
        assert relaxed["valid"] is True
        assert relaxed["labeled"] is False
        assert relaxed["input_count"] == 2

    def test_validation_still_rejects_a_partially_labeled_set(self):
        """A missing baseline for *some* docs is a botched upload, not a flow."""
        s3 = Mock()
        s3.get_paginator.return_value.paginate.side_effect = lambda **kw: (
            [{"Contents": [{"Key": "ts1/input/a.pdf"}, {"Key": "ts1/input/b.pdf"}]}]
            if "input" in kw["Prefix"]
            else [{"Contents": [{"Key": "ts1/baseline/a.pdf/sections/1/result.json"}]}]
        )
        result = test_set_index._validate_test_set_files(
            s3, "bucket", "ts1", allow_unlabeled=True
        )
        assert result["valid"] is False
        assert "b.pdf" in result["error"]

    def test_structure_check_no_longer_requires_a_baseline_folder(self):
        """Discovery must see documents-only sets, not skip them entirely."""
        s3 = Mock()
        s3.head_object.side_effect = Exception("no .uploading marker")
        s3.list_objects_v2.return_value = {"KeyCount": 1}
        assert test_set_index._is_valid_test_set_structure(s3, "bucket", "ts1") is True
        # Only the input/ prefix is consulted now.
        prefixes = [c.kwargs["Prefix"] for c in s3.list_objects_v2.call_args_list]
        assert prefixes == ["ts1/input/"]

    # -- Draft labeling ---------------------------------------------------

    def test_min_confidence_walks_nested_explainability(self):
        """Confidence leaves are nested irregularly; take the true minimum."""
        payload = [
            {
                "vendor": {"confidence": 0.95},
                "line_items": [
                    {"amount": {"confidence": 0.71}},
                    {"amount": {"confidence": 0.88}},
                ],
            }
        ]
        assert test_set_index._min_confidence(payload) == 0.71
        # No confidence anywhere is distinct from confidence 0.
        assert test_set_index._min_confidence({"vendor": {}}) is None
        assert test_set_index._min_confidence(None) is None

    def test_min_confidence_ignores_booleans(self):
        """A bool is an int in Python; it must not be read as a score."""
        assert test_set_index._min_confidence({"f": {"confidence": True}}) is None

    def test_min_confidence_handles_the_real_pipeline_shape(self):
        """Compound fields nest another level (PayPeriod.StartDate on a payslip).

        In explainability_info, confidence sits beside
        confidence_threshold/geometry/ocr_confidence — none of which may be
        mistaken for the score.
        """
        payload = [
            {
                "EmployeeName": {
                    "confidence": 0.999,
                    "confidence_threshold": 0.8,
                    "geometry": [{"boundingBox": {"left": 0.07}, "page": 1}],
                    "geometry_source": "ocr",
                    "ocr_confidence": 0.999,
                },
                "PayPeriod": {
                    "StartDate": {"confidence": 0.994, "confidence_threshold": 0.8},
                    "EndDate": {"confidence": 0.998, "confidence_threshold": 0.8},
                },
            }
        ]
        assert test_set_index._min_confidence(payload) == 0.994
        assert test_set_index._confidence_threshold(payload) == 0.8

    def test_confidence_threshold_tracks_the_weakest_field(self):
        """The reported threshold must belong to the field minConfidence reports."""
        payload = [
            {
                "a": {"confidence": 0.99, "confidence_threshold": 0.5},
                "b": {"confidence": 0.60, "confidence_threshold": 0.9},
            }
        ]
        assert test_set_index._min_confidence(payload) == 0.60
        assert test_set_index._confidence_threshold(payload) == 0.9
        # Absent thresholds stay absent rather than defaulting server-side.
        assert (
            test_set_index._confidence_threshold([{"a": {"confidence": 0.6}}]) is None
        )
        assert test_set_index._confidence_threshold(None) is None

    def test_min_confidence_ignores_fields_the_document_does_not_have(self):
        """Regression: a blank field must not score the whole document 0.0.

        A W-2 with no locality gets confidence 0.0 on locality_name — a correct
        reading of an empty box, not a bad extraction. Taking the raw minimum makes
        a sparsely-populated document score 0.0 while every populated field scores
        above 0.99.
        """
        explainability = [
            {
                "employer_name": {"confidence": 0.997},
                "locality_name": {
                    "confidence": 0.0,
                    "confidence_reason": "No locality name found in OCR results",
                },
                "allocated_tips": {"confidence": 0.0},
            }
        ]
        inference = {
            "employer_name": "CloudNest Technologies, Inc.",
            "locality_name": None,
            "allocated_tips": None,
        }

        # Without the values there is no way to tell absent from uncertain.
        assert test_set_index._min_confidence(explainability) == 0.0
        # With them, the score describes the fields that actually carry data.
        assert test_set_index._min_confidence(explainability, inference) == 0.997

    def test_min_confidence_still_counts_populated_low_confidence_fields(self):
        """Exclusion must not hide genuine uncertainty — only absence."""
        explainability = [
            {
                "good": {"confidence": 0.99},
                "shaky": {"confidence": 0.42},
                "blank": {"confidence": 0.0},
            }
        ]
        inference = {"good": "yes", "shaky": "maybe", "blank": None}
        assert test_set_index._min_confidence(explainability, inference) == 0.42

    def test_min_confidence_reports_zero_when_everything_is_absent(self):
        """An entirely empty extraction is genuinely bad — don't report "no data"."""
        explainability = [{"a": {"confidence": 0.0}, "b": {"confidence": 0.0}}]
        assert (
            test_set_index._min_confidence(explainability, {"a": None, "b": ""}) == 0.0
        )

    def test_min_confidence_treats_empty_containers_as_absent(self):
        explainability = [{"rows": {"confidence": 0.0}, "name": {"confidence": 0.95}}]
        inference = {"rows": [], "name": "Acme"}
        assert test_set_index._min_confidence(explainability, inference) == 0.95

    def test_confidence_threshold_follows_the_same_exclusion(self):
        """The threshold must belong to the field the score now reports."""
        explainability = [
            {
                "blank": {"confidence": 0.0, "confidence_threshold": 0.5},
                "real": {"confidence": 0.8, "confidence_threshold": 0.9},
            }
        ]
        inference = {"blank": None, "real": "x"}
        assert test_set_index._min_confidence(explainability, inference) == 0.8
        assert test_set_index._confidence_threshold(explainability, inference) == 0.9

    def test_alert_counts_uses_each_field_own_threshold(self):
        """A field is an alert relative to its own bar, not a global one.

        0.85 passes under a 0.8 threshold and fails under 0.9, so counting against
        a single constant would contradict the assessment config on one of them.
        """
        explainability = [
            {
                "passes": {"confidence": 0.85, "confidence_threshold": 0.8},
                "fails": {"confidence": 0.85, "confidence_threshold": 0.9},
            }
        ]
        assert test_set_index._alert_counts(explainability) == (1, 2)

    def test_alert_counts_falls_back_to_the_default_threshold(self):
        """Assessment output without thresholds still has to yield a count."""
        explainability = [{"a": {"confidence": 0.95}, "b": {"confidence": 0.5}}]
        assert test_set_index._alert_counts(explainability) == (1, 2)

    def test_alert_counts_excludes_absent_fields(self):
        """Same reason as _min_confidence: a blank box is not an alert.

        Counting it would make every sparsely-populated form look like it needed
        review.
        """
        explainability = [
            {
                "employer_name": {"confidence": 0.997},
                "locality_name": {"confidence": 0.0},
                "shaky": {"confidence": 0.3},
            }
        ]
        inference = {
            "employer_name": "CloudNest",
            "locality_name": None,
            "shaky": "maybe",
        }
        assert test_set_index._alert_counts(explainability) == (2, 3)
        assert test_set_index._alert_counts(explainability, inference) == (1, 2)

    def test_one_blank_table_cell_does_not_excuse_the_whole_column(self):
        """Absent-field exclusion is per occurrence, not per field name.

        Keying on the bare leaf name meant one empty Description in a transaction
        table excluded *every* Description score from minConfidence and alertCount —
        understating review need on precisely the table-heavy documents (bank
        statements) this feature exists for.
        """
        explainability = [
            {
                "Transactions": [
                    {
                        "Description": {"confidence": 0.0},
                        "Amount": {"confidence": 0.99},
                    },
                    {
                        "Description": {"confidence": 0.4},
                        "Amount": {"confidence": 0.98},
                    },
                    {
                        "Description": {"confidence": 0.3},
                        "Amount": {"confidence": 0.97},
                    },
                ]
            }
        ]
        inference = {
            "Transactions": [
                {"Description": None, "Amount": "-44.00"},
                {"Description": "Online Retail", "Amount": "-12.00"},
                {"Description": "Transport", "Amount": "-57.00"},
            ]
        }
        # Row 0's blank Description is excluded; rows 1 and 2 still alert.
        alerts, fields = test_set_index._alert_counts(explainability, inference)
        assert (alerts, fields) == (2, 5)
        # And the weakest *populated* field drives the headline, not the blank one.
        assert test_set_index._min_confidence(explainability, inference) == 0.3

    def test_alert_counts_reports_none_without_confidence_data(self):
        """None means "no confidence data", which is not the same as zero alerts."""
        assert test_set_index._alert_counts(None) == (None, None)
        assert test_set_index._alert_counts({"vendor": {}}) == (None, None)

    def test_generate_draft_labels_delegates_to_the_test_runner(self, labeling_env):
        table, _ = labeling_env
        _seed_test_set(table, "ts1", fileCount=2)

        lambda_client = MagicMock()
        lambda_client.invoke.return_value = {
            "Payload": Mock(read=lambda: json.dumps({"testRunId": "ts1-run"}).encode())
        }
        with patch.object(test_set_index.boto3, "client", return_value=lambda_client):
            result = test_set_index.generate_draft_labels(
                {"input": {"testSetId": "ts1"}},
                {"identity": {"claims": {"email": "me@example.com"}}},
            )

        assert result["jobId"] == "ts1-run"
        assert result["status"] == "RUNNING"
        assert result["total"] == 2
        # The run is created by the test runner (one owner of config capture and
        # version pinning), invoked without an identity as a trusted service call.
        payload = json.loads(lambda_client.invoke.call_args.kwargs["Payload"])
        assert payload["info"]["fieldName"] == "startTestRun"
        assert payload["arguments"]["input"]["testSetId"] == "ts1"
        assert "identity" not in payload
        # Job item recorded under the test set, and the set marked as labeling.
        job = table.get_item(Key={"PK": "testset#ts1", "SK": "labeljob#ts1-run"})[
            "Item"
        ]
        assert job["startedBy"] == "me@example.com"
        meta = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert meta["labelJobStatus"] == "RUNNING"

    def test_generate_draft_labels_rejects_an_empty_test_set(self, labeling_env):
        table, _ = labeling_env
        _seed_test_set(table, "ts1", fileCount=0)
        with pytest.raises(Exception, match="no documents to label"):
            test_set_index.generate_draft_labels({"input": {"testSetId": "ts1"}})

    def test_harvest_writes_draft_labels_with_confidence(self, labeling_env):
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        uri = _seed_pipeline_result(
            s3,
            "ts1-run/a.pdf/sections/1/result.json",
            {"vendor": "Acme"},
            [{"vendor": {"confidence": 0.42, "confidence_threshold": 0.8}}],
        )
        _seed_completed_run(
            table,
            "ts1-run",
            "ts1",
            ["a.pdf"],
            {"a.pdf": [{"Id": "1", "OutputJSONUri": uri}]},
        )
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#ts1-run",
                "testSetId": "ts1",
                "jobId": "ts1-run",
                "status": "RUNNING",
                "total": 1,
                "labeled": 0,
            }
        )

        result = test_set_index.get_draft_label_job(
            {"testSetId": "ts1", "jobId": "ts1-run"}
        )

        assert result["status"] == "COMPLETED"
        assert result["labeled"] == 1
        # Written to the baseline layout the GT editor and scoring already read.
        body = json.loads(
            s3.get_object(
                Bucket="test-set-bucket",
                Key="ts1/baseline/a.pdf/sections/1/result.json",
            )["Body"].read()
        )
        assert body["inference_result"] == {"vendor": "Acme"}
        assert body["labelSource"] == "draft-machine"
        assert body["minConfidence"] == 0.42
        assert body["confidenceThreshold"] == 0.8
        meta = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert meta["labelState"] == "draft"

    def test_harvest_never_overwrites_a_human_reviewed_label(self, labeling_env):
        """Re-running draft labeling must not destroy confirmed ground truth."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        reviewed = {
            "inference_result": {"vendor": "Corrected By Human"},
            "labelSource": "reviewed-human",
        }
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=json.dumps(reviewed).encode(),
        )
        uri = _seed_pipeline_result(
            s3, "ts1-run/a.pdf/sections/1/result.json", {"vendor": "Machine Guess"}
        )
        _seed_completed_run(
            table,
            "ts1-run",
            "ts1",
            ["a.pdf"],
            {"a.pdf": [{"Id": "1", "OutputJSONUri": uri}]},
        )
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#ts1-run",
                "testSetId": "ts1",
                "jobId": "ts1-run",
                "status": "RUNNING",
                "total": 1,
                "labeled": 0,
            }
        )

        test_set_index.get_draft_label_job({"testSetId": "ts1", "jobId": "ts1-run"})

        body = json.loads(
            s3.get_object(
                Bucket="test-set-bucket",
                Key="ts1/baseline/a.pdf/sections/1/result.json",
            )["Body"].read()
        )
        assert body["inference_result"] == {"vendor": "Corrected By Human"}
        assert body["labelSource"] == "reviewed-human"

    def test_harvest_treats_an_uploaded_baseline_as_human_owned(self, labeling_env):
        """A hand-uploaded baseline has no labelSource; never silently replace it."""
        table, s3 = labeling_env
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=json.dumps({"inference_result": {"vendor": "Uploaded GT"}}).encode(),
        )
        assert (
            test_set_index._existing_label_is_human(
                "test-set-bucket", "ts1/baseline/a.pdf/sections/1/result.json"
            )
            is True
        )
        # But a missing label is fair game.
        assert (
            test_set_index._existing_label_is_human("test-set-bucket", "ts1/nope.json")
            is False
        )
        # And a previous machine draft is replaceable, so re-running picks up a
        # newer config.
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/b.pdf/sections/1/result.json",
            Body=json.dumps({"labelSource": "draft-machine"}).encode(),
        )
        assert (
            test_set_index._existing_label_is_human(
                "test-set-bucket", "ts1/baseline/b.pdf/sections/1/result.json"
            )
            is False
        )

    def test_a_failed_document_does_not_hold_the_job_open_forever(self, labeling_env):
        """A terminal document is resolved-with-error, not pending.

        Counting it as pending kept the job RUNNING indefinitely, and every caller
        that displays a job drives the harvest on a timer — so the workspace
        re-polled every 5s forever, each tick re-reading the set, and the "labeling
        in progress" banner never cleared.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=2)
        uri = _seed_pipeline_result(
            s3, "ts1-run/a.pdf/sections/1/result.json", {"vendor": "Acme"}
        )
        _seed_completed_run(
            table,
            "ts1-run",
            "ts1",
            ["a.pdf", "b.pdf"],
            {"a.pdf": [{"Id": "1", "OutputJSONUri": uri}]},
        )
        table.put_item(
            Item={
                "PK": "doc#ts1-run/b.pdf",
                "SK": "none",
                "ObjectStatus": "FAILED",
                "Sections": [],
            }
        )
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#ts1-run",
                "testSetId": "ts1",
                "jobId": "ts1-run",
                "status": "RUNNING",
                "total": 2,
                "labeled": 0,
            }
        )

        result = test_set_index.get_draft_label_job(
            {"testSetId": "ts1", "jobId": "ts1-run"}
        )
        assert result["status"] == "COMPLETED"
        assert result["labeled"] == 1
        # The count is what explains why labeled < total.
        assert result["failedDocuments"] == 1
        # And it is not retried on the next poll.
        job = table.get_item(Key={"PK": "testset#ts1", "SK": "labeljob#ts1-run"})[
            "Item"
        ]
        assert job["failedFiles"] == ["b.pdf"]

    def test_a_job_whose_every_document_failed_is_marked_failed(self, labeling_env):
        """Nothing harvested and nothing left to wait for is a failure, not success."""
        table, _ = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        _seed_completed_run(table, "ts1-run", "ts1", ["a.pdf"], {})
        table.put_item(
            Item={
                "PK": "doc#ts1-run/a.pdf",
                "SK": "none",
                "ObjectStatus": "ABORTED",
                "Sections": [],
            }
        )
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#ts1-run",
                "testSetId": "ts1",
                "jobId": "ts1-run",
                "status": "RUNNING",
                "total": 1,
                "labeled": 0,
            }
        )

        result = test_set_index.get_draft_label_job(
            {"testSetId": "ts1", "jobId": "ts1-run"}
        )
        assert result["status"] == "FAILED"
        assert "failed processing" in (result["error"] or "")
        # labelState must not advance to "draft" — there are no drafts.
        meta = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert meta.get("labelState") != "draft"

    def test_a_missing_tracking_record_is_pending_until_the_job_is_stale(
        self, labeling_env
    ):
        """Absence cannot be told from "not started yet", so it waits — for a while."""
        table, _ = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        # Deliberately no doc# item for a.pdf: the copy never produced one.
        table.put_item(
            Item={
                "PK": "testrun#ts1-run",
                "SK": "metadata",
                "TestSetId": "ts1",
                "Files": ["a.pdf"],
                "Status": "RUNNING",
            }
        )
        job_item = {
            "PK": "testset#ts1",
            "SK": "labeljob#ts1-run",
            "testSetId": "ts1",
            "jobId": "ts1-run",
            "status": "RUNNING",
            "total": 1,
            "labeled": 0,
            # Relative, not hardcoded: a fixed date silently ages past the
            # staleness window and the test starts asserting the opposite case.
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        table.put_item(Item=job_item)
        assert (
            test_set_index.get_draft_label_job(
                {"testSetId": "ts1", "jobId": "ts1-run"}
            )["status"]
            == "RUNNING"
        )

        # Same job, long past any plausible processing time.
        job_item["createdAt"] = (
            (
                datetime.now(timezone.utc)
                - timedelta(hours=test_set_index.STALE_LABEL_JOB_HOURS + 1)
            )
            .isoformat()
            .replace("+00:00", "Z")
        )
        job_item["status"] = "RUNNING"
        table.put_item(Item=job_item)
        result = test_set_index.get_draft_label_job(
            {"testSetId": "ts1", "jobId": "ts1-run"}
        )
        assert result["status"] == "FAILED"
        assert result["failedDocuments"] == 1

    def test_harvest_stays_running_while_documents_are_pending(self, labeling_env):
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=2)
        uri = _seed_pipeline_result(
            s3, "ts1-run/a.pdf/sections/1/result.json", {"vendor": "Acme"}
        )
        _seed_completed_run(
            table,
            "ts1-run",
            "ts1",
            ["a.pdf", "b.pdf"],
            {"a.pdf": [{"Id": "1", "OutputJSONUri": uri}]},
        )
        # b.pdf hasn't finished processing yet.
        table.put_item(
            Item={
                "PK": "doc#ts1-run/b.pdf",
                "SK": "none",
                "ObjectStatus": "RUNNING",
                "Sections": [],
            }
        )
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#ts1-run",
                "testSetId": "ts1",
                "jobId": "ts1-run",
                "status": "RUNNING",
                "total": 2,
                "labeled": 0,
            }
        )

        result = test_set_index.get_draft_label_job(
            {"testSetId": "ts1", "jobId": "ts1-run"}
        )
        assert result["status"] == "RUNNING"
        assert result["labeled"] == 1

    def test_harvest_does_not_re_read_documents_it_already_copied(self, labeling_env):
        """Each poll must only do the work that is new.

        Harvesting is several S3 calls per section, and every caller that shows a
        job drives it. Re-copying finished documents made a large set slower to
        harvest the closer it got to done.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=2)
        uris = {
            name: _seed_pipeline_result(
                s3, f"ts1-run/{name}/sections/1/result.json", {"vendor": name}
            )
            for name in ("a.pdf", "b.pdf")
        }
        _seed_completed_run(
            table,
            "ts1-run",
            "ts1",
            ["a.pdf", "b.pdf"],
            {name: [{"Id": "1", "OutputJSONUri": uri}] for name, uri in uris.items()},
        )
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#ts1-run",
                "testSetId": "ts1",
                "jobId": "ts1-run",
                "status": "RUNNING",
                "total": 2,
                "labeled": 0,
                # a.pdf was copied by an earlier poll.
                "harvestedFiles": ["a.pdf"],
            }
        )

        read_keys = []
        real_get = test_set_index.s3_client.get_object

        def spy(**kwargs):
            read_keys.append(kwargs.get("Key"))
            return real_get(**kwargs)

        test_set_index.s3_client.get_object = spy
        try:
            result = test_set_index.get_draft_label_job(
                {"testSetId": "ts1", "jobId": "ts1-run"}
            )
        finally:
            test_set_index.s3_client.get_object = real_get

        assert not any("a.pdf" in key for key in read_keys), read_keys
        assert any("b.pdf" in key for key in read_keys), read_keys
        # Progress still counts the whole set, not just this pass.
        assert result["status"] == "COMPLETED"
        assert result["labeled"] == 2
        job = table.get_item(Key={"PK": "testset#ts1", "SK": "labeljob#ts1-run"})[
            "Item"
        ]
        assert sorted(job["harvestedFiles"]) == ["a.pdf", "b.pdf"]

    def test_harvest_stops_at_its_deadline_and_stays_resumable(self, labeling_env):
        """A set too large for one pass must make partial progress, not time out.

        The resolver has 60s. Stopping short has to leave the job RUNNING with the
        finished documents recorded, so the next poll continues rather than
        restarting.
        """
        table, s3 = labeling_env
        names = ["a.pdf", "b.pdf", "c.pdf"]
        _seed_test_set(table, "ts1", fileCount=len(names))
        sections = {}
        for name in names:
            uri = _seed_pipeline_result(
                s3, f"ts1-run/{name}/sections/1/result.json", {"vendor": name}
            )
            sections[name] = [{"Id": "1", "OutputJSONUri": uri}]
        _seed_completed_run(table, "ts1-run", "ts1", names, sections)
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#ts1-run",
                "testSetId": "ts1",
                "jobId": "ts1-run",
                "status": "RUNNING",
                "total": len(names),
                "labeled": 0,
            }
        )
        job = table.get_item(Key={"PK": "testset#ts1", "SK": "labeljob#ts1-run"})[
            "Item"
        ]

        # The budget runs out after the first document.
        ticks = []

        def clock():
            ticks.append(None)
            return 0.0 if len(ticks) == 1 else 100.0

        with patch.object(test_set_index.time, "monotonic", clock):
            first = test_set_index._harvest_label_job(job, deadline=1.0)

        assert first["status"] == "RUNNING"
        assert first["labeled"] == 1
        assert first["harvestedFiles"] == ["a.pdf"]

        # No deadline on the follow-up: it finishes the remaining two.
        second = test_set_index._harvest_label_job(
            table.get_item(Key={"PK": "testset#ts1", "SK": "labeljob#ts1-run"})["Item"]
        )
        assert second["status"] == "COMPLETED"
        assert second["labeled"] == len(names)
        for name in names:
            s3.head_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/{name}/sections/1/result.json",
            )

    def test_harvest_marks_the_job_failed_when_the_run_fails(self, labeling_env):
        table, _ = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        table.put_item(
            Item={
                "PK": "testrun#ts1-run",
                "SK": "metadata",
                "TestSetId": "ts1",
                "Files": ["a.pdf"],
                "Status": "FAILED",
                "Error": "pipeline exploded",
            }
        )
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#ts1-run",
                "testSetId": "ts1",
                "jobId": "ts1-run",
                "status": "RUNNING",
                "total": 1,
                "labeled": 0,
            }
        )

        result = test_set_index.get_draft_label_job(
            {"testSetId": "ts1", "jobId": "ts1-run"}
        )
        assert result["status"] == "FAILED"
        assert result["error"] == "pipeline exploded"
        meta = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert meta["labelJobStatus"] == "FAILED"

    def test_get_draft_label_job_unknown_job_raises(self, labeling_env):
        table, _ = labeling_env
        _seed_test_set(table, "ts1")
        with pytest.raises(Exception, match="Labeling job 'nope' not found"):
            test_set_index.get_draft_label_job({"testSetId": "ts1", "jobId": "nope"})

    def test_attach_label_metadata_takes_the_worst_field_and_source(self, labeling_env):
        """A document's confidence is its weakest field, across all sections."""
        _, s3 = labeling_env
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=json.dumps(
                {
                    "labelSource": "reviewed-human",
                    "explainability_info": [
                        {"f": {"confidence": 0.99, "confidence_threshold": 0.9}}
                    ],
                }
            ).encode(),
        )
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/2/result.json",
            Body=json.dumps(
                {
                    "labelSource": "draft-machine",
                    "explainability_info": [
                        {"f": {"confidence": 0.31, "confidence_threshold": 0.7}}
                    ],
                }
            ).encode(),
        )
        documents = [
            {
                "objectKey": "a.pdf",
                "sections": [
                    {
                        "sectionId": "1",
                        "baselineKey": "ts1/baseline/a.pdf/sections/1/result.json",
                    },
                    {
                        "sectionId": "2",
                        "baselineKey": "ts1/baseline/a.pdf/sections/2/result.json",
                    },
                ],
            },
            {"objectKey": "b.pdf", "sections": []},
        ]

        test_set_index._attach_label_metadata("test-set-bucket", documents)

        # Any draft section means the document is not fully reviewed.
        assert documents[0]["labelSource"] == "draft-machine"
        assert documents[0]["minConfidence"] == 0.31
        # The threshold reported is the weakest field's (section 2), not section 1's.
        assert documents[0]["confidenceThreshold"] == 0.7
        # No sections at all = unlabeled, not "confident".
        assert documents[1]["labelSource"] is None
        assert documents[1]["minConfidence"] is None
        assert documents[1]["confidenceThreshold"] is None

    # -- Review-effort estimator -------------------------------------------

    def test_estimate_review_effort_reports_prior_on_a_cold_set(self, labeling_env):
        """With no curve and no labels, the estimate must not look measured."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=2)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/b.pdf", Body=b"x")

        result = test_set_index.estimate_review_effort(
            {"testSetId": "ts1", "targetAccuracy": 99.0}
        )

        assert result["estimateConfidence"] == "prior"
        assert result["totalDocs"] == 2
        # A prior-driven estimate reports a range, not a bare point value.
        assert result["docsToReviewLow"] <= result["docsToReview"]
        assert result["docsToReviewHigh"] >= result["docsToReview"]
        assert result["calibration"]["totalObservations"] == 0

    def test_estimate_review_effort_uses_the_stored_curve(self, labeling_env):
        """Observations recorded from review must change the estimate."""
        from idp_common.evaluation.curve_store import CurveStore

        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")

        # A review pass found the 0.3-confidence band is mostly wrong.
        CurveStore(table).add_observations("ts1", [(0.3, False)] * 40)

        result = test_set_index.estimate_review_effort({"testSetId": "ts1"})
        assert result["calibration"]["totalObservations"] == 40
        assert result["estimateConfidence"] in (
            "partially-measured",
            "unreliable",
        )

    def test_estimate_review_effort_recommends_reviewing_everything_when_overconfident(
        self, labeling_env
    ):
        """The dangerous quadrant must not yield a small, confident-looking number."""
        from idp_common.evaluation.curve_store import CurveStore

        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        # Confident and wrong.
        CurveStore(table).add_observations("ts1", [(0.95, False)] * 60)

        result = test_set_index.estimate_review_effort({"testSetId": "ts1"})
        assert result["recommendReviewAll"] is True
        assert result["estimateConfidence"] == "unreliable"
        assert result["calibration"]["overconfident"] is True

    def test_estimate_review_effort_validates_its_inputs(self, labeling_env):
        table, _ = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        with pytest.raises(Exception, match="targetAccuracy"):
            test_set_index.estimate_review_effort(
                {"testSetId": "ts1", "targetAccuracy": 150}
            )
        with pytest.raises(Exception, match="not found"):
            test_set_index.estimate_review_effort({"testSetId": "ghost"})

    def test_estimate_review_effort_includes_the_reliability_table(self, labeling_env):
        """The curve must be inspectable, not just a number."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")

        result = test_set_index.estimate_review_effort({"testSetId": "ts1"})
        assert len(result["reliabilityTable"]) == 10
        assert "burndown" in result

    # -- Annotation queue --------------------------------------------------

    def test_annotation_queue_is_worst_first(self, labeling_env):
        """Lowest confidence first — each review removes the most error."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=3)
        for name, conf in (("a.pdf", 0.95), ("b.pdf", 0.20), ("c.pdf", 0.60)):
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/{name}/sections/1/result.json",
                Body=json.dumps(
                    {
                        "labelSource": "draft-machine",
                        "explainability_info": [{"f": {"confidence": conf}}],
                    }
                ).encode(),
            )

        result = test_set_index.get_annotation_queue({"testSetId": "ts1"}, None)

        order = [d["objectKey"] for d in result["documents"]]
        assert order == ["b.pdf", "c.pdf", "a.pdf"], order
        assert result["nextObjectKey"] == "b.pdf"
        assert result["totalDocs"] == 3
        assert result["remainingDocs"] == 3

    def test_annotation_queue_puts_unlabeled_documents_first(self, labeling_env):
        """An unlabeled document is the least trustworthy, not the most."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=2)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/labeled.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/labeled.pdf/sections/1/result.json",
            Body=json.dumps(
                {
                    "labelSource": "draft-machine",
                    "explainability_info": [{"f": {"confidence": 0.1}}],
                }
            ).encode(),
        )
        # No baseline at all for this one.
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/bare.pdf", Body=b"x")

        result = test_set_index.get_annotation_queue({"testSetId": "ts1"}, None)
        assert result["documents"][0]["objectKey"] == "bare.pdf"

    def test_annotation_queue_excludes_reviewed_documents(self, labeling_env):
        """Reviewed work drops out of the queue but still counts as progress."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=2)
        for name, source in (
            ("done.pdf", "reviewed-human"),
            ("todo.pdf", "draft-machine"),
        ):
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/{name}/sections/1/result.json",
                Body=json.dumps(
                    {
                        "labelSource": source,
                        "explainability_info": [{"f": {"confidence": 0.4}}],
                    }
                ).encode(),
            )

        result = test_set_index.get_annotation_queue({"testSetId": "ts1"}, None)
        assert [d["objectKey"] for d in result["documents"]] == ["todo.pdf"]
        assert result["reviewedDocs"] == 1
        assert result["remainingDocs"] == 1

        # ...and can be included explicitly for a progress view.
        withall = test_set_index.get_annotation_queue(
            {"testSetId": "ts1", "includeCompleted": True}, None
        )
        assert len(withall["documents"]) == 2

    def test_annotation_queue_reflects_another_annotators_claim(self, labeling_env):
        """A claimed doc must drop out of everyone else's 'next in queue'."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=2, labelJobId="run1")
        for name, conf in (("claimed.pdf", 0.1), ("free.pdf", 0.5)):
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/{name}/sections/1/result.json",
                Body=json.dumps(
                    {
                        "labelSource": "draft-machine",
                        "explainability_info": [{"f": {"confidence": conf}}],
                    }
                ).encode(),
            )
        # Someone else holds the lowest-confidence document.
        table.put_item(
            Item={
                "PK": "doc#run1/claimed.pdf",
                "SK": "none",
                "HITLReviewOwner": "other@example.com",
                "HITLStatus": "InProgress",
            }
        )

        event = {
            "identity": {
                "claims": {"cognito:groups": ["Admin"], "email": "me@example.com"}
            }
        }
        result = test_set_index.get_annotation_queue({"testSetId": "ts1"}, event)

        by_key = {d["objectKey"]: d for d in result["documents"]}
        assert by_key["claimed.pdf"]["claimedBy"] == "other@example.com"
        assert by_key["claimed.pdf"]["available"] is False
        assert by_key["claimed.pdf"]["claimedByMe"] is False
        # Still worst-first in the listing, but "next" skips to what I can take.
        assert result["nextObjectKey"] == "free.pdf"
        assert result["claimedByOthers"] == 1

    def test_annotation_queue_marks_my_own_claim_as_available(self, labeling_env):
        """Resuming my own in-progress document must not be blocked."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1, labelJobId="run1")
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/mine.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/mine.pdf/sections/1/result.json",
            Body=json.dumps(
                {
                    "labelSource": "draft-machine",
                    "explainability_info": [{"f": {"confidence": 0.3}}],
                }
            ).encode(),
        )
        table.put_item(
            Item={
                "PK": "doc#run1/mine.pdf",
                "SK": "none",
                "HITLReviewOwner": "me@example.com",
                "HITLStatus": "InProgress",
            }
        )

        event = {
            "identity": {
                "claims": {"cognito:groups": ["Admin"], "email": "me@example.com"}
            }
        }
        result = test_set_index.get_annotation_queue({"testSetId": "ts1"}, event)
        item = result["documents"][0]
        assert item["claimedByMe"] is True
        assert item["available"] is True
        assert result["nextObjectKey"] == "mine.pdf"

    def test_annotation_queue_denies_an_out_of_scope_annotator(self, labeling_env):
        """Scope is checked before the set is read, so nothing leaks."""
        from idp_common import testset_scope

        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        testset_scope.clear_scope_cache()

        event = {
            "identity": {
                "claims": {
                    "cognito:groups": ["Annotator"],
                    "email": "ann@example.com",
                }
            }
        }
        # No users table configured -> annotator has no resolvable scope -> denied.
        with pytest.raises(Exception, match="Unauthorized"):
            test_set_index.get_annotation_queue({"testSetId": "ts1"}, event)
        testset_scope.clear_scope_cache()

    def test_annotation_queue_validates_the_test_set_id(self, labeling_env):
        table, _ = labeling_env
        with pytest.raises(Exception, match="Invalid test set id"):
            test_set_index.get_annotation_queue({"testSetId": "../etc/passwd"}, None)

    def test_estimate_reports_the_real_set_size_not_the_sample_size(
        self, labeling_env, monkeypatch
    ):
        """Regression: a large set must not report its sampling cap as its size.

        Reporting MAX_DOCS_FOR_ESTIMATE as totalDocs understates the review work,
        the effort and the audit pool several-fold. fileCount is the set's size; the
        sampled confidences are only how much of it was inspected.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=2008)
        # Only three documents actually exist in S3 to sample from.
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/{name}/sections/1/result.json",
                Body=json.dumps(
                    {
                        "labelSource": "draft-machine",
                        "explainability_info": [{"f": {"confidence": 0.5}}],
                    }
                ).encode(),
            )

        result = test_set_index.estimate_review_effort({"testSetId": "ts1"})

        assert result["totalDocs"] == 2008
        assert result["sampledDocs"] == 3
        # Review depth and audit pool are bounded by the real size, not the sample.
        assert result["docsToReview"] <= 2008
        assert result["docsToReviewHigh"] <= 2008
        assert len(result["burndown"]) == 2009  # 0..N inclusive

    def test_estimate_stops_sampling_before_the_lambda_times_out(
        self, labeling_env, monkeypatch
    ):
        """Regression: the estimate must return a narrower answer, never nothing.

        Each document's sections are read from S3 to recover their confidence, so a
        large sample costs multiple sequential pages of S3 reads and can exceed the
        Lambda timeout. A time budget bounds the paging independently of the
        document cap: a smaller sample still yields a usable estimate, and
        sampledDocs reports how much was inspected.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=600)
        for i in range(5):
            name = f"doc{i}.pdf"
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/{name}/sections/1/result.json",
                Body=json.dumps(
                    {
                        "labelSource": "draft-machine",
                        "explainability_info": [{"f": {"confidence": 0.5}}],
                    }
                ).encode(),
            )

        # Every page appears to take longer than the whole budget.
        monkeypatch.setattr(test_set_index, "SAMPLING_TIME_BUDGET_SECONDS", 0)

        result = test_set_index.estimate_review_effort({"testSetId": "ts1"})

        # It returned rather than paging on: an answer, plus how much it saw.
        assert result["totalDocs"] == 600
        assert result["sampledDocs"] >= 1
        assert result["docsToReview"] <= 600

    def test_queue_stops_collecting_before_the_lambda_times_out(
        self, labeling_env, monkeypatch
    ):
        """The workspace must open with a short queue rather than not at all.

        Same S3-read cost as the estimator, but this is the page an annotator lands
        on, so a timeout here means they cannot work at all. A truncated queue is
        fine — they take documents from the front — and inspectedDocs reports how
        much was ranked.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=600)
        for i in range(4):
            name = f"doc{i}.pdf"
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/{name}/sections/1/result.json",
                Body=json.dumps(
                    {
                        "labelSource": "draft-machine",
                        "explainability_info": [{"f": {"confidence": 0.4}}],
                    }
                ).encode(),
            )

        monkeypatch.setattr(test_set_index, "SAMPLING_TIME_BUDGET_SECONDS", 0)

        result = test_set_index.get_annotation_queue({"testSetId": "ts1"}, None)

        assert result["totalDocs"] == 600
        assert result["inspectedDocs"] >= 1
        assert len(result["documents"]) >= 1

    def test_field_sample_is_bounded_when_no_baseline_carries_fields(
        self, labeling_env, monkeypatch
    ):
        """Regression: the effort model's field sample must bound its S3 reads.

        Split-only ground truth has an empty ``inference_result``, so every section
        yields no field count. A cap on collected counts therefore never trips and
        the loop reads every section in the set — one GET each — until the Lambda
        times out. Bounding reads instead keeps it finite.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=30)
        reads = []
        for i in range(30):
            name = f"packet_{i:04d}.pdf"
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
            # Six sections each, none carrying extractable fields.
            for sec in range(1, 7):
                s3.put_object(
                    Bucket="test-set-bucket",
                    Key=f"ts1/baseline/{name}/sections/{sec}/result.json",
                    Body=json.dumps(
                        {"document_class": {"type": "packet"}, "inference_result": {}}
                    ).encode(),
                )

        real = test_set_index._count_baseline_fields

        def counting(bucket, key):
            reads.append(key)
            return real(bucket, key)

        monkeypatch.setattr(test_set_index, "_count_baseline_fields", counting)

        test_set_index.estimate_review_effort({"testSetId": "ts1"})

        # 180 sections exist; the sample must stop well short of reading them all.
        assert len(reads) <= test_set_index.MAX_SECTIONS_FOR_FIELD_SAMPLE, (
            f"read {len(reads)} sections; the field sample is unbounded"
        )

    def test_sampling_cap_fits_in_one_page(self):
        """The cap must not require multiple sequential pages.

        get_test_set_documents pages at 200, and each page costs a full S3 read of
        every section on it, so a cap above the page size multiplies the wall-clock
        cost of the estimate.
        """
        assert test_set_index.MAX_DOCS_FOR_ESTIMATE <= 200

    def test_estimate_sampled_equals_total_for_a_small_set(self, labeling_env):
        """No extrapolation when every document was inspected."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=2)
        for name in ("a.pdf", "b.pdf"):
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")

        result = test_set_index.estimate_review_effort({"testSetId": "ts1"})
        assert result["totalDocs"] == 2
        assert result["sampledDocs"] == 2

    def test_uploaded_ground_truth_is_not_counted_as_review_work(self, labeling_env):
        """Regression: a set that arrived with labels is not 100% annotated.

        Baselines with no labelSource must not default to reviewed-human, which
        would report the whole set reviewed with an empty queue. Uploaded ground
        truth is authoritative — draft labeling still won't overwrite it — but
        nobody reviewed it here, so it cannot claim annotation progress.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=2)
        for name in ("a.pdf", "b.pdf"):
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
            # No labelSource key at all — how an uploaded baseline arrives.
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/{name}/sections/1/result.json",
                Body=json.dumps({"inference_result": {"f": "v"}}).encode(),
            )

        result = test_set_index.get_annotation_queue({"testSetId": "ts1"}, None)

        assert result["reviewedDocs"] == 0
        assert result["remainingDocs"] == 2
        assert len(result["documents"]) == 2
        assert result["nextObjectKey"] is not None
        # Reported as uploaded, distinct from a label a human reviewed here.
        assert result["documents"][0]["labelSource"] == "uploaded"
        assert result["documents"][0]["reviewed"] is False

    def test_uploaded_ground_truth_is_still_protected_from_overwrite(
        self, labeling_env
    ):
        """The relabel guard must not weaken just because progress changed.

        Overwrite safety keys on the label being an explicit draft, not on it
        being reviewed-human, so an untagged uploaded baseline stays protected.
        """
        _, s3 = labeling_env
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=json.dumps({"inference_result": {"f": "uploaded gt"}}).encode(),
        )
        assert (
            test_set_index._existing_label_is_human(
                "test-set-bucket", "ts1/baseline/a.pdf/sections/1/result.json"
            )
            is True
        )

    def test_queue_reports_the_real_set_size_not_the_inspected_page(self, labeling_env):
        """Regression: the queue cap must not be reported as the set size.

        Same conflation as the estimator: reporting the cap as totalDocs makes
        reviewing the first page read as "0 remaining" with most of the set
        untouched.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=2008)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")

        result = test_set_index.get_annotation_queue({"testSetId": "ts1"}, None)

        assert result["totalDocs"] == 2008
        assert result["inspectedDocs"] == 1
        assert result["remainingDocs"] == 2008

    def test_queue_returns_the_review_object_key(self, labeling_env):
        """The UI must not rebuild the pipeline key shape itself.

        claimReview/completeSectionReview key on "{runId}/{filename}", not the
        test-set key. Returning it keeps that layout a backend detail.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1, labelJobId="ts1-run-1")
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        # The pipeline copy must exist: a review key is only offered for documents
        # the run actually processed.
        table.put_item(
            Item={
                "PK": "doc#ts1-run-1/a.pdf",
                "SK": "none",
                "ObjectStatus": "COMPLETED",
            }
        )

        result = test_set_index.get_annotation_queue({"testSetId": "ts1"}, None)
        assert result["documents"][0]["reviewObjectKey"] == "ts1-run-1/a.pdf"

    def test_queue_review_key_is_null_without_a_labeling_run(self, labeling_env):
        """No pipeline copy exists yet, so there is nothing to claim."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)  # no labelJobId
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")

        result = test_set_index.get_annotation_queue({"testSetId": "ts1"}, None)
        assert result["documents"][0]["reviewObjectKey"] is None

    def test_queue_batches_claim_reads(self, labeling_env):
        """Claim state must not cost one round-trip per document.

        Batched reads have to preserve per-document attribution, so this checks
        the claim lands on the right document across a multi-batch (>100) read.
        """
        table, s3 = labeling_env
        names = [f"doc_{i:04d}.pdf" for i in range(120)]
        _seed_test_set(table, "ts1", fileCount=len(names), labelJobId="run1")
        for name in names:
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
        # Claim one document in the second batch.
        claimed = names[110]
        table.put_item(
            Item={
                "PK": f"doc#run1/{claimed}",
                "SK": "none",
                "HITLReviewOwner": "other@example.com",
                "HITLStatus": "InProgress",
            }
        )

        result = test_set_index.get_annotation_queue(
            {"testSetId": "ts1", "limit": 200}, None
        )
        by_key = {d["objectKey"]: d for d in result["documents"]}
        assert by_key[claimed]["claimedBy"] == "other@example.com"
        assert by_key[claimed]["available"] is False
        # Every other document is untouched.
        assert result["claimedByOthers"] == 1
        assert by_key[names[0]]["claimedBy"] is None

    def test_draft_labeling_skips_documents_that_already_have_ground_truth(
        self, labeling_env
    ):
        """A mixed set must only label the documents that need it.

        Generated and uploaded ground truth carries no labelSource, which the
        overwrite guard treats as protected, so labeling them would run inference
        and discard the result — wasted spend on a mixed set, and an entirely
        pointless run on a fully generated one.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=3)
        # Two documents already carry ground truth, one is bare.
        for name in ("gt1.pdf", "gt2.pdf", "bare.pdf"):
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
        for name in ("gt1.pdf", "gt2.pdf"):
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/{name}/sections/1/result.json",
                Body=json.dumps({"inference_result": {"box_a": "authored"}}).encode(),
            )

        with patch.object(test_set_index, "boto3") as mock_boto3:
            mock_lambda = MagicMock()
            mock_lambda.invoke.return_value = {
                "Payload": MagicMock(read=lambda: b'{"testRunId": "ts1-run"}')
            }
            mock_boto3.client.return_value = mock_lambda
            result = test_set_index.generate_draft_labels({"testSetId": "ts1"})

        payload = json.loads(mock_lambda.invoke.call_args.kwargs["Payload"])
        requested = payload["arguments"]["input"]["objectKeys"]
        assert requested == ["bare.pdf"], requested
        assert result["total"] == 1
        assert result["skippedAlreadyLabeled"] == 2

    def test_draft_labeling_refuses_a_fully_labeled_set(self, labeling_env):
        """Generated sets are already ground truth — say so instead of no-op'ing."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/gt.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/gt.pdf/sections/1/result.json",
            Body=json.dumps({"inference_result": {"box_a": "authored"}}).encode(),
        )

        with pytest.raises(Exception, match="already has ground truth"):
            test_set_index.generate_draft_labels({"testSetId": "ts1"})

    def test_draft_labeling_still_relabels_prior_drafts(self, labeling_env):
        """A machine draft is replaceable, so re-running on a better config works."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=json.dumps(
                {"labelSource": "draft-machine", "inference_result": {"f": 1}}
            ).encode(),
        )

        with patch.object(test_set_index, "boto3") as mock_boto3:
            mock_lambda = MagicMock()
            mock_lambda.invoke.return_value = {
                "Payload": MagicMock(read=lambda: b'{"testRunId": "ts1-run"}')
            }
            mock_boto3.client.return_value = mock_lambda
            result = test_set_index.generate_draft_labels({"testSetId": "ts1"})

        payload = json.loads(mock_lambda.invoke.call_args.kwargs["Payload"])
        assert payload["arguments"]["input"]["objectKeys"] == ["a.pdf"]
        assert result["skippedAlreadyLabeled"] == 0

    def test_reextract_pins_the_corrected_class_and_labels_one_document(
        self, labeling_env
    ):
        """Correcting the class must actually reach the extraction.

        Two things have to happen, and this test previously asserted only the
        first — which is why the flow shipped broken. The baseline pin is not
        sufficient on its own: the run classifies from the *input document* and
        never reads the test set's baseline, so the pipeline re-derived the
        original class and the harvest wrote it back over the pin.

        So the class must also be sent INTO the run, which stamps it as S3
        metadata for the classification step. Only the named document is
        processed.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=2)
        for name in ("check.pdf", "other.pdf"):
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/check.pdf/sections/1/result.json",
            Body=json.dumps(
                {
                    "labelSource": "draft-machine",
                    "document_class": {"type": "bank-statement"},
                    "inference_result": {"account_number": "123"},
                }
            ).encode(),
        )

        with patch.object(test_set_index, "boto3") as mock_boto3:
            mock_lambda = MagicMock()
            mock_lambda.invoke.return_value = {
                "Payload": MagicMock(read=lambda: b'{"testRunId": "ts1-reextract"}')
            }
            mock_boto3.client.return_value = mock_lambda
            result = test_set_index.reextract_test_set_document(
                {
                    "input": {
                        "testSetId": "ts1",
                        "objectKey": "check.pdf",
                        "documentClass": "bank-check",
                    }
                }
            )

        assert result["jobId"] == "ts1-reextract"
        payload = json.loads(mock_lambda.invoke.call_args.kwargs["Payload"])
        assert payload["arguments"]["input"]["objectKeys"] == ["check.pdf"]
        # The assertion whose absence let the bug ship: without this the class
        # never reaches the pipeline and the correction is silently discarded.
        assert payload["arguments"]["input"]["documentClass"] == "bank-check"

        written = json.loads(
            s3.get_object(
                Bucket="test-set-bucket",
                Key="ts1/baseline/check.pdf/sections/1/result.json",
            )["Body"].read()
        )
        assert written["document_class"]["type"] == "bank-check"

    def test_reextract_demotes_a_reviewed_label_so_the_harvest_can_replace_it(
        self, labeling_env
    ):
        """The one place a reviewed label is deliberately downgraded.

        The harvest refuses to overwrite reviewed-human labels, so without demoting
        them a re-extraction of an already-confirmed document would report success
        while leaving the wrong-class fields in place. Re-extracting after a class
        correction is itself a statement that the current labels are wrong.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/check.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/check.pdf/sections/1/result.json",
            Body=json.dumps(
                {
                    "labelSource": "reviewed-human",
                    "document_class": {"type": "bank-statement"},
                    "inference_result": {"account_number": "123"},
                }
            ).encode(),
        )

        with patch.object(test_set_index, "boto3") as mock_boto3:
            mock_lambda = MagicMock()
            mock_lambda.invoke.return_value = {
                "Payload": MagicMock(read=lambda: b'{"testRunId": "ts1-reextract"}')
            }
            mock_boto3.client.return_value = mock_lambda
            test_set_index.reextract_test_set_document(
                {
                    "input": {
                        "testSetId": "ts1",
                        "objectKey": "check.pdf",
                        "documentClass": "bank-check",
                    }
                }
            )

        written = json.loads(
            s3.get_object(
                Bucket="test-set-bucket",
                Key="ts1/baseline/check.pdf/sections/1/result.json",
            )["Body"].read()
        )
        assert written["labelSource"] == "draft-machine"
        assert written["document_class"]["type"] == "bank-check"

    def test_reextract_leaves_other_documents_reviewable(self, labeling_env):
        """Regression: re-extracting one document disabled annotation for the set.

        Review keys are ``{runId}/{filename}``. Re-extract runs as a one-document
        labeling job, and the set carried a single labelJobId that the queue used
        for every document — so after one Annotator used "fix class & re-extract",
        every OTHER unreviewed document resolved through that one-document run,
        found no pipeline copy, and reported "not ready to annotate". That defeats
        the collaborative queue this exists to serve, and it is reachable by an
        Annotator in the normal flow.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=3)
        names = ["a.pdf", "b.pdf", "c.pdf"]
        for name in names:
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/{name}/sections/1/result.json",
                Body=json.dumps(
                    {
                        "labelSource": "draft-machine",
                        "explainability_info": [{"f": {"confidence": 0.5}}],
                        "inference_result": {"f": "v"},
                    }
                ).encode(),
            )

        # A full labeling run covered all three, and its pipeline copies exist.
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#run-full",
                "jobId": "run-full",
                "status": "COMPLETED",
                "objectKeys": names,
                "createdAt": "2026-01-01T00:00:00Z",
            }
        )
        table.update_item(
            Key={"PK": "testset#ts1", "SK": "metadata"},
            UpdateExpression="SET labelJobId = :j",
            ExpressionAttributeValues={":j": "run-full"},
        )
        for name in names:
            table.put_item(Item={"PK": f"doc#run-full/{name}", "SK": "none"})

        # Now a.pdf is re-extracted: a one-document job, with its own copy.
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#run-one",
                "jobId": "run-one",
                "status": "COMPLETED",
                "objectKeys": ["a.pdf"],
                "createdAt": "2026-01-02T00:00:00Z",
            }
        )
        table.put_item(Item={"PK": "doc#run-one/a.pdf", "SK": "none"})
        table.update_item(
            Key={"PK": "testset#ts1", "SK": "metadata"},
            UpdateExpression="SET labelJobId = :j",
            ExpressionAttributeValues={":j": "run-one"},
        )

        result = test_set_index.get_annotation_queue({"testSetId": "ts1"}, None)
        by_key = {d["objectKey"]: d for d in result["documents"]}

        # The re-extracted document points at its newest run...
        assert by_key["a.pdf"]["reviewObjectKey"] == "run-one/a.pdf"
        # ...and its neighbours keep the run that actually produced their copies.
        for name in ("b.pdf", "c.pdf"):
            assert by_key[name]["reviewObjectKey"] == f"run-full/{name}", (
                f"{name} lost its review key after an unrelated re-extract"
            )
            assert by_key[name]["available"] is True

    def test_harvest_covers_a_run_the_pointer_no_longer_names(self, labeling_env):
        """A one-document re-extract must not orphan a full run still in flight.

        Harvesting only the job the set's pointer names left the remaining
        documents of an in-progress full run permanently unharvested.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        for sk, job_id, keys, created in (
            ("labeljob#run-full", "run-full", ["a.pdf"], "2026-01-01T00:00:00Z"),
            ("labeljob#run-one", "run-one", ["a.pdf"], "2026-01-02T00:00:00Z"),
        ):
            table.put_item(
                Item={
                    "PK": "testset#ts1",
                    "SK": sk,
                    "jobId": job_id,
                    "status": "RUNNING",
                    "objectKeys": keys,
                    "createdAt": created,
                    "total": 1,
                    "labeled": 0,
                }
            )
        table.update_item(
            Key={"PK": "testset#ts1", "SK": "metadata"},
            UpdateExpression="SET labelJobId = :j",
            ExpressionAttributeValues={":j": "run-one"},
        )

        harvested = []
        real = test_set_index._harvest_label_job

        def spy(job, **_kwargs):
            harvested.append(job.get("jobId"))
            return job

        test_set_index._harvest_label_job = spy
        try:
            meta = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
            test_set_index._harvest_active_label_job("ts1", meta)
        finally:
            test_set_index._harvest_label_job = real

        assert set(harvested) == {"run-full", "run-one"}, harvested

    def test_reextract_never_demotes_authored_ground_truth(self, labeling_env):
        """Regression: re-extraction must not demote a supplied verified label.

        A baseline with no labelSource was supplied when the test set was created —
        nobody predicted it, so there is nothing for a re-extraction to correct, and
        demoting it would let the harvest replace it with a machine guess. Only a
        reviewed-human label is demoted (that one IS a prediction someone confirmed,
        and the annotator has just said it is wrong). The class correction lands
        either way.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/gt.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/gt.pdf/sections/1/result.json",
            # No labelSource: authored ground truth.
            Body=json.dumps(
                {
                    "document_class": {"type": "bank-statement"},
                    "inference_result": {"verified_field": "authoritative"},
                }
            ).encode(),
        )

        with patch.object(test_set_index, "boto3") as mock_boto3:
            mock_lambda = MagicMock()
            mock_lambda.invoke.return_value = {
                "Payload": MagicMock(read=lambda: b'{"testRunId": "r"}')
            }
            mock_boto3.client.return_value = mock_lambda
            test_set_index.reextract_test_set_document(
                {
                    "input": {
                        "testSetId": "ts1",
                        "objectKey": "gt.pdf",
                        "documentClass": "bank-check",
                    }
                }
            )

        written = json.loads(
            s3.get_object(
                Bucket="test-set-bucket",
                Key="ts1/baseline/gt.pdf/sections/1/result.json",
            )["Body"].read()
        )
        # The correction lands...
        assert written["document_class"]["type"] == "bank-check"
        # ...but the label keeps its provenance, so the harvest leaves it alone and
        # the verified values survive.
        assert "labelSource" not in written or written["labelSource"] is None
        assert written["inference_result"]["verified_field"] == "authoritative"

    def test_reextract_without_a_class_leaves_the_existing_one(self, labeling_env):
        """Re-running under the same class is legitimate (e.g. a config fix)."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/check.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/check.pdf/sections/1/result.json",
            Body=json.dumps(
                {
                    "labelSource": "reviewed-human",
                    "document_class": {"type": "bank-check"},
                }
            ).encode(),
        )

        with patch.object(test_set_index, "boto3") as mock_boto3:
            mock_lambda = MagicMock()
            mock_lambda.invoke.return_value = {
                "Payload": MagicMock(read=lambda: b'{"testRunId": "r"}')
            }
            mock_boto3.client.return_value = mock_lambda
            test_set_index.reextract_test_set_document(
                {"input": {"testSetId": "ts1", "objectKey": "check.pdf"}}
            )

        written = json.loads(
            s3.get_object(
                Bucket="test-set-bucket",
                Key="ts1/baseline/check.pdf/sections/1/result.json",
            )["Body"].read()
        )
        # No class given, so nothing is rewritten — including the review tag.
        assert written["labelSource"] == "reviewed-human"
        assert written["document_class"]["type"] == "bank-check"

    def test_an_annotator_can_watch_the_reextract_they_started(self):
        """Starting a job without being able to observe it is not a capability.

        The editor calls reextractTestSetDocument — long Annotator-reachable — and
        then polls getDraftLabelJob for the outcome. getDraftLabelJob was absent from
        ANNOTATOR_ALLOWED_FIELDS, so on a dev stack an annotator's class correction
        ran to completion server-side while the UI showed "Could not re-extract this
        document": a failure message over a job that worked.
        """
        allowed = test_set_index.ANNOTATOR_ALLOWED_FIELDS

        # Both halves of the same flow, or neither is usable.
        assert "reextractTestSetDocument" in allowed
        assert "getDraftLabelJob" in allowed

    def test_every_annotator_field_asserts_per_set_scope(self):
        """Group membership alone would expose other teams' sets.

        Each Annotator-reachable field must assert per-set access somewhere on its
        path. Checked in the dispatch branch AND in the handler it calls, because the
        codebase does both: getTestSetDocuments asserts in the dispatch,
        getAnnotationQueue inside its handler. Looking only at the dispatch reported a
        gap that was not there.
        """
        source = pathlib.Path(test_set_index.__file__).read_text(encoding="utf-8")
        dispatch = source[source.index("def handler(") :]

        for field in test_set_index.ANNOTATOR_ALLOWED_FIELDS:
            branch_at = dispatch.index(f'field_name == "{field}"')
            next_branch = dispatch.find("elif field_name ==", branch_at + 1)
            branch = dispatch[
                branch_at : next_branch if next_branch > 0 else len(dispatch)
            ]
            if "assert_can_access_test_set" in branch:
                continue

            # Otherwise the handler it delegates to has to do it.
            called = re.findall(r"return (\w+)\(", branch)
            assert called, f"{field} dispatches to nothing recognisable"
            handler_src = ""
            for name in called:
                at = source.find(f"def {name}(")
                if at == -1:
                    continue
                end = source.find("\ndef ", at + 1)
                handler_src += source[at : end if end > 0 else len(source)]
            assert "assert_can_access_test_set" in handler_src, (
                f"{field} is Annotator-reachable but neither its dispatch branch nor "
                f"{called} asserts per-set access"
            )

    # ---------------------------------------------------------------- regrouping

    @staticmethod
    def _seed_packet(s3, sections):
        """Write a document's baseline sections. `sections` is {id: (class, indices)}."""
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/packet.pdf", Body=b"x")
        for section_id, (doc_class, indices) in sections.items():
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/packet.pdf/sections/{section_id}/result.json",
                Body=json.dumps(
                    {
                        "document_class": {"type": doc_class},
                        "split_document": {"page_indices": indices},
                        "inference_result": {"field": f"value-from-{section_id}"},
                        "labelSource": "reviewed-human",
                        "_editHistory": [{"editedBy": "someone"}],
                    }
                ).encode(),
            )

    @staticmethod
    def _read_sections(s3):
        """{section_id: parsed result.json} for the seeded document."""
        prefix = "ts1/baseline/packet.pdf/sections/"
        out = {}
        for obj in s3.list_objects_v2(Bucket="test-set-bucket", Prefix=prefix).get(
            "Contents", []
        ):
            if not obj["Key"].endswith("/result.json"):
                continue
            section_id = obj["Key"][len(prefix) :].split("/")[0]
            out[section_id] = json.loads(
                s3.get_object(Bucket="test-set-bucket", Key=obj["Key"])["Body"].read()
            )
        return out

    def test_regrouping_preserves_every_field_value(self, labeling_env):
        """The whole reason this mutation exists rather than a re-extract.

        The motivating case was a wrong packet split on a document carrying
        annotations that could not be lost. Re-running extraction would fix the grouping and destroy the
        annotations, so this writes the grouping and nothing else.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        self._seed_packet(s3, {"1": ("FieldTicket", [0, 1]), "2": ("Invoice", [2, 3])})

        test_set_index.update_test_set_document_sections(
            {
                "input": {
                    "testSetId": "ts1",
                    "objectKey": "packet.pdf",
                    "sections": [
                        {
                            "sectionId": "1",
                            "documentClass": "FieldTicket",
                            "pageIndices": [0, 1, 2],
                        },
                        {
                            "sectionId": "2",
                            "documentClass": "Invoice",
                            "pageIndices": [3],
                        },
                    ],
                }
            }
        )

        after = self._read_sections(s3)
        assert after["1"]["split_document"]["page_indices"] == [0, 1, 2]
        assert after["2"]["split_document"]["page_indices"] == [3]
        # Untouched: the values, their provenance, and the edit trail.
        assert after["1"]["inference_result"] == {"field": "value-from-1"}
        assert after["2"]["inference_result"] == {"field": "value-from-2"}
        assert after["1"]["labelSource"] == "reviewed-human"
        assert after["1"]["_editHistory"] == [{"editedBy": "someone"}]

    # ------------------------------------------- annotation drafts and versioning

    @staticmethod
    def _baseline_keys(s3, prefix):
        # Paginated, because this helper is used to check a >1000-object snapshot and a
        # bare list_objects_v2 caps at 1000 — the same trap the production copy has to
        # avoid, which this helper originally fell into and reported as a product bug.
        keys = []
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket="test-set-bucket", Prefix=prefix):
            keys.extend(o["Key"] for o in page.get("Contents", []) or [])
        return sorted(keys)

    def _seed_labelled_set(self, table, s3, test_set_id="ts1", value="original"):
        """A set with ground truth already in place, as an uploaded one arrives."""
        _seed_test_set(table, test_set_id, fileCount=1)
        s3.put_object(
            Bucket="test-set-bucket", Key=f"{test_set_id}/input/a.pdf", Body=b"x"
        )
        s3.put_object(
            Bucket="test-set-bucket",
            Key=f"{test_set_id}/baseline/a.pdf/sections/1/result.json",
            Body=json.dumps({"inference_result": {"total": value}}).encode(),
        )

    def test_opening_a_draft_preserves_the_labels_it_moves_away_from(
        self, labeling_env
    ):
        """The property the whole operation exists for.

        A version was a DynamoDB row that copied nothing, so annotating in place changed
        what every recorded version number referred to and a scored run could not be
        reproduced. Starting annotation "effectively commit[s] that we'll be
        creating a new version" — so the state being left has to be captured.
        """
        table, s3 = labeling_env
        self._seed_labelled_set(table, s3)

        result = test_set_index.open_test_set_annotation_draft(
            {"input": {"testSetId": "ts1"}}
        )

        assert result["baseVersion"] == 1
        assert result["draftVersion"] == 2
        assert result["snapshotObjectCount"] == 1
        # The bytes, not just the number.
        snapshot = self._baseline_keys(s3, "ts1/versions/1/baseline/")
        assert snapshot == ["ts1/versions/1/baseline/a.pdf/sections/1/result.json"]

    def test_a_later_annotation_does_not_change_the_snapshot(self, labeling_env):
        """Reproducibility, stated as a test: the snapshot must not track the live
        labels, or the version number is decoration again."""
        table, s3 = labeling_env
        self._seed_labelled_set(table, s3, value="original")
        test_set_index.open_test_set_annotation_draft({"input": {"testSetId": "ts1"}})

        # The reviewer corrects the document, exactly as the editor's presigned POST does.
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=json.dumps({"inference_result": {"total": "corrected"}}).encode(),
        )

        frozen = json.loads(
            s3.get_object(
                Bucket="test-set-bucket",
                Key="ts1/versions/1/baseline/a.pdf/sections/1/result.json",
            )["Body"].read()
        )
        assert frozen["inference_result"]["total"] == "original"

    def test_a_set_with_no_published_version_gets_its_arriving_labels_captured(
        self, labeling_env
    ):
        """The "even if we still have ground truth" case.

        An uploaded set has never been published, so without this the labels it arrived
        with would be the ones overwritten, with nothing recording what they were.
        """
        table, s3 = labeling_env
        self._seed_labelled_set(table, s3)

        result = test_set_index.open_test_set_annotation_draft(
            {"input": {"testSetId": "ts1"}}
        )

        meta = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert int(meta["publishedVersion"]) == 1
        assert result["baseVersion"] == 1
        versions = test_set_index.get_test_set_versions({"testSetId": "ts1"})
        assert [v["label"] for v in versions] == ["As uploaded"]

    def test_opening_a_draft_does_not_move_the_active_reference(self, labeling_env):
        """An Annotator may open a transition; deciding which version every future run
        of the set scores against is publishTestSetVersion's, Admin/Author only. The
        internal publish of v1 therefore leaves activeReference alone — runs default
        to current labels and pin only when asked, so nothing depends on it here."""
        table, s3 = labeling_env
        self._seed_labelled_set(table, s3)

        test_set_index.open_test_set_annotation_draft({"input": {"testSetId": "ts1"}})

        meta = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert int(meta["publishedVersion"]) == 1
        assert meta.get("activeReference") is None

    def test_a_draft_still_closes_when_a_newer_publish_already_won(self, labeling_env):
        """The REMOVE rode on the conditional pointer update and was lost with it,
        leaving the annotate view reporting an open transition toward a version that
        already existed."""
        table, s3 = labeling_env
        self._seed_labelled_set(table, s3)
        test_set_index.open_test_set_annotation_draft({"input": {"testSetId": "ts1"}})
        # A concurrent publish advanced the pointer past what this one will reserve
        # (latestVersion + 1 = 2), so the conditional pointer update loses.
        table.update_item(
            Key={"PK": "testset#ts1", "SK": "metadata"},
            UpdateExpression="SET publishedVersion = :v",
            ExpressionAttributeValues={":v": 9},
        )

        test_set_index.publish_test_set_version(
            {"input": {"testSetId": "ts1", "label": "later"}}
        )

        meta = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert int(meta["publishedVersion"]) == 9, "the newer pointer must win"
        assert "draftVersion" not in meta

    def test_the_draft_number_follows_the_counter_publish_uses(self, labeling_env):
        """publish reserves latestVersion + 1, and a failed version write leaves a gap
        by design, so publishedVersion + 1 can name a version the next publish will
        never create — and the badge and ?v= link would name it too."""
        table, s3 = labeling_env
        self._seed_labelled_set(table, s3)
        test_set_index.publish_test_set_version(
            {"input": {"testSetId": "ts1", "label": "v1"}}
        )
        table.update_item(
            Key={"PK": "testset#ts1", "SK": "metadata"},
            UpdateExpression="SET latestVersion = :v",
            ExpressionAttributeValues={":v": 3},
        )

        result = test_set_index.open_test_set_annotation_draft(
            {"input": {"testSetId": "ts1"}}
        )

        assert result["baseVersion"] == 1
        assert result["draftVersion"] == 4

    def test_an_oversize_set_is_refused_with_a_reason_and_no_pointer(
        self, labeling_env, monkeypatch
    ):
        """The pointer is written after the copy, so a set too large for one request
        used to fail with a generic error and fail identically on every retry."""
        table, s3 = labeling_env
        self._seed_labelled_set(table, s3)
        monkeypatch.setattr(test_set_index, "_SNAPSHOT_MAX_OBJECTS", 0)

        with pytest.raises(Exception, match="more than the 0"):
            test_set_index.open_test_set_annotation_draft(
                {"input": {"testSetId": "ts1"}}
            )

        meta = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert "draftVersion" not in meta
        assert self._baseline_keys(s3, "ts1/versions/1/baseline/") == []

    def test_opening_a_draft_twice_is_idempotent(self, labeling_env):
        """The annotate view calls this on entry, so a second call must not open a
        second transition or re-copy thousands of objects."""
        table, s3 = labeling_env
        self._seed_labelled_set(table, s3)

        first = test_set_index.open_test_set_annotation_draft(
            {"input": {"testSetId": "ts1"}}
        )
        second = test_set_index.open_test_set_annotation_draft(
            {"input": {"testSetId": "ts1"}}
        )

        assert second["draftVersion"] == first["draftVersion"]
        assert second["baseVersion"] == first["baseVersion"]
        assert second["alreadyOpen"] is True
        assert second["snapshotObjectCount"] == 0

    def test_the_draft_is_recorded_on_the_set(self, labeling_env):
        # The queue link is built from this, so it has to be readable afterwards.
        table, s3 = labeling_env
        self._seed_labelled_set(table, s3)

        test_set_index.open_test_set_annotation_draft({"input": {"testSetId": "ts1"}})

        meta = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert int(meta["draftVersion"]) == 2

    def test_publishing_closes_the_open_draft(self, labeling_env):
        """Otherwise the view reports a transition toward a version that already exists,
        and the next session never snapshots."""
        table, s3 = labeling_env
        self._seed_labelled_set(table, s3)
        test_set_index.open_test_set_annotation_draft({"input": {"testSetId": "ts1"}})

        test_set_index.publish_test_set_version({"input": {"testSetId": "ts1"}})

        meta = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert "draftVersion" not in meta

    def test_a_second_session_after_publishing_snapshots_again(self, labeling_env):
        # v2 is now published, so the next transition is 2 -> 3 and preserves v2.
        table, s3 = labeling_env
        self._seed_labelled_set(table, s3)
        test_set_index.open_test_set_annotation_draft({"input": {"testSetId": "ts1"}})
        test_set_index.publish_test_set_version({"input": {"testSetId": "ts1"}})

        result = test_set_index.open_test_set_annotation_draft(
            {"input": {"testSetId": "ts1"}}
        )

        assert (result["baseVersion"], result["draftVersion"]) == (2, 3)
        assert self._baseline_keys(s3, "ts1/versions/2/baseline/")

    def test_the_snapshot_copies_every_object_past_a_pagination_boundary(
        self, labeling_env
    ):
        """list_objects_v2 pages at 1000. A 2000-document set is real (Fake-W2), so an
        unpaginated copy would silently preserve only the first page of a version."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        for i in range(1005):
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/doc{i:04d}.pdf/sections/1/result.json",
                Body=b"{}",
            )

        result = test_set_index.open_test_set_annotation_draft(
            {"input": {"testSetId": "ts1"}}
        )

        assert result["snapshotObjectCount"] == 1005
        assert len(self._baseline_keys(s3, "ts1/versions/1/baseline/")) == 1005

    def test_the_queue_reports_the_open_transition(self, labeling_env):
        """So the workspace can tell "in a transition" from "not started" on entry.

        Without this the only way to find out would be calling
        openTestSetAnnotationDraft — which snapshots, so probing with it would open a
        transition merely by visiting the page. That is the silent commitment the whole
        mechanism exists to remove, so the read has to be a read.
        """
        table, s3 = labeling_env
        self._seed_labelled_set(table, s3)

        before = test_set_index.get_annotation_queue({"testSetId": "ts1"}, None)
        assert before["draftVersion"] is None
        assert before["baseVersion"] is None

        test_set_index.open_test_set_annotation_draft({"input": {"testSetId": "ts1"}})

        after = test_set_index.get_annotation_queue({"testSetId": "ts1"}, None)
        assert after["draftVersion"] == 2
        assert after["baseVersion"] == 1

    def test_the_queue_stops_reporting_a_transition_once_published(self, labeling_env):
        table, s3 = labeling_env
        self._seed_labelled_set(table, s3)
        test_set_index.open_test_set_annotation_draft({"input": {"testSetId": "ts1"}})

        test_set_index.publish_test_set_version({"input": {"testSetId": "ts1"}})

        queue = test_set_index.get_annotation_queue({"testSetId": "ts1"}, None)
        assert queue["draftVersion"] is None

    def test_an_unknown_test_set_is_refused(self, labeling_env):
        _table, _s3 = labeling_env

        with pytest.raises(Exception, match="not found"):
            test_set_index.open_test_set_annotation_draft(
                {"input": {"testSetId": "nope"}}
            )

    def test_the_operation_is_annotator_reachable_and_scope_checked(self, labeling_env):
        # Both halves matter: an annotator starts the session, and group membership alone
        # would let them open a transition on a set they were never assigned.
        assert "openTestSetAnnotationDraft" in test_set_index.ANNOTATOR_ALLOWED_FIELDS
        source = pathlib.Path(test_set_index.__file__).read_text(encoding="utf-8")
        branch = source.split('elif field_name == "openTestSetAnnotationDraft":')[1]
        branch = branch.split("elif field_name ==")[0]
        assert "assert_can_access_test_set" in branch

    # ------------------------------------------------- zip upload: the set's name

    def test_the_supplied_name_is_used_not_the_zip_filename(self, labeling_env):
        """The reported bug: type a name, get the archive's.

        `TestSetUploadInput` carried no `name` at all, so the wizard collected and
        validated a name it had nowhere to send, and the resolver derived one from the
        filename. There were no tests for this resolver, which is how it survived.
        """
        table, _s3 = labeling_env

        result = test_set_index.add_test_set_from_upload(
            {
                "input": {
                    "fileName": "Archive.zip",
                    "fileSize": 1024,
                    "name": "my-test-set",
                    "description": "",
                }
            }
        )

        assert result["testSetId"] == "my-test-set"
        item = table.get_item(Key={"PK": "testset#my-test-set", "SK": "metadata"})[
            "Item"
        ]
        assert item["name"] == "my-test-set"

    def test_a_name_with_spaces_becomes_a_slugged_id(self, labeling_env):
        table, _s3 = labeling_env

        result = test_set_index.add_test_set_from_upload(
            {"input": {"fileName": "Archive.zip", "fileSize": 1, "name": "My Test Set"}}
        )

        assert result["testSetId"] == "my-test-set"
        item = table.get_item(Key={"PK": "testset#my-test-set", "SK": "metadata"})[
            "Item"
        ]
        # The display name keeps what was typed; only the id is slugged.
        assert item["name"] == "My Test Set"

    def test_no_name_still_falls_back_to_the_filename(self, labeling_env):
        # Kept working on purpose: the field is optional because this mutation has
        # shipped since 0.6.1, so a direct caller that sends no name must not break.
        table, _s3 = labeling_env

        result = test_set_index.add_test_set_from_upload(
            {"input": {"fileName": "Archive.zip", "fileSize": 1}}
        )

        assert result["testSetId"] == "archive"
        assert (
            table.get_item(Key={"PK": "testset#archive", "SK": "metadata"})["Item"][
                "name"
            ]
            == "Archive"
        )

    def test_a_blank_name_falls_back_rather_than_creating_an_empty_id(
        self, labeling_env
    ):
        # `name: "   "` from a form is not a name.
        _table, _s3 = labeling_env

        result = test_set_index.add_test_set_from_upload(
            {"input": {"fileName": "Archive.zip", "fileSize": 1, "name": "   "}}
        )

        assert result["testSetId"] == "archive"

    def test_an_uppercase_extension_still_yields_a_clean_fallback_name(
        self, labeling_env
    ):
        # Stripping the suffix by length handles any case; the previous code needed a
        # `.replace` per spelling and would have missed e.g. ".Zip".
        _table, _s3 = labeling_env

        result = test_set_index.add_test_set_from_upload(
            {"input": {"fileName": "Archive.ZIP", "fileSize": 1}}
        )

        assert result["testSetId"] == "archive"

    def test_a_supplied_name_that_is_not_a_valid_set_name_is_refused(
        self, labeling_env
    ):
        # The name now reaching validation is the caller's, so its rejection has to name
        # the caller's problem rather than a filename they did not choose.
        _table, _s3 = labeling_env

        with pytest.raises(Exception, match="letters, numbers, spaces"):
            test_set_index.add_test_set_from_upload(
                {
                    "input": {
                        "fileName": "Archive.zip",
                        "fileSize": 1,
                        "name": "bad/name",
                    }
                }
            )

    def test_a_repeated_section_id_is_refused(self, labeling_env):
        """Sections are keyed by id when the existing baselines are looked up, so a
        repeated id writes the same baseline content into two section files and
        renumbers around a group that no longer exists. The page-level checks cannot
        catch it: both groups' pages are legitimately accounted for."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        self._seed_packet(s3, {"1": ("FieldTicket", [0, 1]), "2": ("Invoice", [2])})

        with pytest.raises(Exception, match="more than once"):
            test_set_index.update_test_set_document_sections(
                {
                    "input": {
                        "testSetId": "ts1",
                        "objectKey": "packet.pdf",
                        "sections": [
                            {
                                "sectionId": "1",
                                "documentClass": "FieldTicket",
                                "pageIndices": [0],
                            },
                            {
                                "sectionId": "1",
                                "documentClass": "Invoice",
                                "pageIndices": [1],
                            },
                            {
                                "sectionId": "2",
                                "documentClass": "Invoice",
                                "pageIndices": [2],
                            },
                        ],
                    }
                }
            )

        # Nothing written: the refusal happens in validation, before any put_object.
        after = self._read_sections(s3)
        assert after["1"]["split_document"]["page_indices"] == [0, 1]
        assert after["2"]["split_document"]["page_indices"] == [2]

    def test_a_custom_page_order_survives_the_save(self, labeling_env):
        """The defect this pair of tests exists for.

        ``page_indices`` records the section's reading order as well as its membership:
        ``split_accuracy_with_order`` compares the two lists with ``==`` and half the
        graded packet score is Kendall's Tau over each page's position
        (``stickler_backend/doc_split.py:111``). This once called ``sorted()`` here, so a
        reviewer's authored order was discarded on every save — and the value written was
        still plausible-looking JSON, so nothing raised.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        self._seed_packet(s3, {"1": ("FieldTicket", [0, 1]), "2": ("Invoice", [2, 3])})

        test_set_index.update_test_set_document_sections(
            {
                "input": {
                    "testSetId": "ts1",
                    "objectKey": "packet.pdf",
                    "sections": [
                        # Page 1 read before page 0: a packet assembled out of order.
                        {
                            "sectionId": "1",
                            "documentClass": "FieldTicket",
                            "pageIndices": [1, 0],
                        },
                        {
                            "sectionId": "2",
                            "documentClass": "Invoice",
                            "pageIndices": [3, 2],
                        },
                    ],
                }
            }
        )

        after = self._read_sections(s3)
        assert after["1"]["split_document"]["page_indices"] == [1, 0]
        assert after["2"]["split_document"]["page_indices"] == [3, 2]
        # And the values are still preserved alongside the order.
        assert after["1"]["inference_result"] == {"field": "value-from-1"}
        assert after["1"]["labelSource"] == "reviewed-human"

    def test_the_response_reports_the_order_actually_written(self, labeling_env):
        """The client redraws its board from this, so a sorted response would show the
        reviewer an order that is not what is on disk."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        self._seed_packet(s3, {"1": ("Invoice", [0, 1, 2])})

        result = test_set_index.update_test_set_document_sections(
            {
                "input": {
                    "testSetId": "ts1",
                    "objectKey": "packet.pdf",
                    "sections": [
                        {
                            "sectionId": "1",
                            "documentClass": "Invoice",
                            "pageIndices": [2, 0, 1],
                        }
                    ],
                }
            }
        )

        assert result["sections"][0]["pageIndices"] == [2, 0, 1]
        assert self._read_sections(s3)["1"]["split_document"]["page_indices"] == [
            2,
            0,
            1,
        ]

    def test_a_class_only_edit_does_not_normalise_an_existing_order(self, labeling_env):
        """The worst shape of the bug: a reviewer who touched only the *class* would have
        silently rewritten the section's page order as a side effect, changing a metric
        they never went near."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        self._seed_packet(s3, {"1": ("FieldTicket", [2, 0, 1])})

        test_set_index.update_test_set_document_sections(
            {
                "input": {
                    "testSetId": "ts1",
                    "objectKey": "packet.pdf",
                    "sections": [
                        {
                            "sectionId": "1",
                            "documentClass": "Invoice",
                            "pageIndices": [2, 0, 1],
                        }
                    ],
                }
            }
        )

        after = self._read_sections(s3)
        assert after["1"]["document_class"]["type"] == "Invoice"
        assert after["1"]["split_document"]["page_indices"] == [2, 0, 1]

    def test_section_order_keys_on_the_lowest_page_not_the_first_listed(
        self, labeling_env
    ):
        """Now that a section can carry a manual page order, ``min`` and ``[0]`` differ.

        A section's place in the document is where it *starts*, so section ordering keys on
        the lowest page. Keying on the first listed page would let a within-section reorder
        silently renumber the sections around it.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        self._seed_packet(s3, {"1": ("FieldTicket", [0, 1]), "2": ("Invoice", [2, 3])})

        result = test_set_index.update_test_set_document_sections(
            {
                "input": {
                    "testSetId": "ts1",
                    "objectKey": "packet.pdf",
                    "sections": [
                        # Reads page 3 first, but still starts at page 2.
                        {
                            "sectionId": "2",
                            "documentClass": "Invoice",
                            "pageIndices": [3, 2],
                        },
                        {
                            "sectionId": "1",
                            "documentClass": "FieldTicket",
                            "pageIndices": [0, 1],
                        },
                    ],
                }
            }
        )

        # FieldTicket still comes first: it starts at page 0.
        assert [s["documentClass"] for s in result["sections"]] == [
            "FieldTicket",
            "Invoice",
        ]

    def test_sections_are_renumbered_so_ids_agree_with_page_order(self, labeling_env):
        """Consumers take a section's group index from its position in a list.

        `compute_graded_packet_metrics` enumerates the sections it is given, and nothing
        guarantees that list is in page order. Numbering 1..N by first page makes id
        order, lexical key order and page order the same thing, so no consumer can
        disagree about which group a page belongs to.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        self._seed_packet(s3, {"1": ("FieldTicket", [2, 3]), "2": ("Invoice", [0, 1])})

        result = test_set_index.update_test_set_document_sections(
            {
                "input": {
                    "testSetId": "ts1",
                    "objectKey": "packet.pdf",
                    "sections": [
                        {
                            "sectionId": "1",
                            "documentClass": "FieldTicket",
                            "pageIndices": [2, 3],
                        },
                        {
                            "sectionId": "2",
                            "documentClass": "Invoice",
                            "pageIndices": [0, 1],
                        },
                    ],
                }
            }
        )

        after = self._read_sections(s3)
        # Section 1 now holds the FIRST pages, and carries the content that came with
        # them — the values follow their pages, not their old id.
        assert after["1"]["split_document"]["page_indices"] == [0, 1]
        assert after["1"]["inference_result"] == {"field": "value-from-2"}
        assert after["2"]["split_document"]["page_indices"] == [2, 3]
        assert after["2"]["inference_result"] == {"field": "value-from-1"}
        assert [s["pageIndices"] for s in result["sections"]] == [[0, 1], [2, 3]]

    def test_merging_two_sections_removes_the_leftover_file(self, labeling_env):
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        self._seed_packet(s3, {"1": ("FieldTicket", [0, 1]), "2": ("Invoice", [2, 3])})

        test_set_index.update_test_set_document_sections(
            {
                "input": {
                    "testSetId": "ts1",
                    "objectKey": "packet.pdf",
                    "sections": [
                        {
                            "sectionId": "1",
                            "documentClass": "FieldTicket",
                            "pageIndices": [0, 1, 2, 3],
                        }
                    ],
                }
            }
        )

        after = self._read_sections(s3)
        # A stale section/2/result.json left behind would be read as a real section by
        # every consumer, including scoring.
        assert list(after) == ["1"]

    def test_a_new_section_is_written_with_no_field_values(self, labeling_env):
        """Splitting one section into two: the new half has nothing extracted yet.

        Writing an empty inference_result states that plainly; inventing a copy of the
        original's values would look like extracted data that no model produced.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        self._seed_packet(s3, {"1": ("FieldTicket", [0, 1, 2, 3])})

        test_set_index.update_test_set_document_sections(
            {
                "input": {
                    "testSetId": "ts1",
                    "objectKey": "packet.pdf",
                    "sections": [
                        {
                            "sectionId": "1",
                            "documentClass": "FieldTicket",
                            "pageIndices": [0, 1],
                        },
                        {
                            "sectionId": "99",
                            "documentClass": "Invoice",
                            "pageIndices": [2, 3],
                        },
                    ],
                }
            }
        )

        after = self._read_sections(s3)
        assert after["1"]["inference_result"] == {"field": "value-from-1"}
        assert after["2"]["inference_result"] == {}
        assert after["2"]["document_class"]["type"] == "Invoice"

    def test_a_page_in_two_sections_is_refused(self, labeling_env):
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        self._seed_packet(s3, {"1": ("FieldTicket", [0, 1]), "2": ("Invoice", [2, 3])})

        with pytest.raises(Exception, match="in both section"):
            test_set_index.update_test_set_document_sections(
                {
                    "input": {
                        "testSetId": "ts1",
                        "objectKey": "packet.pdf",
                        "sections": [
                            {"sectionId": "1", "pageIndices": [0, 1, 2]},
                            {"sectionId": "2", "pageIndices": [2, 3]},
                        ],
                    }
                }
            )

    def test_dropping_a_labelled_page_is_refused(self, labeling_env):
        """Losing a page silently would discard the ground truth for it.

        The client also requires every page of the rendered PDF to be assigned, which
        only it can check — the server would have to parse the document to learn its
        page count. What the server can enforce is that nothing already labelled goes
        missing, which is the half that loses data.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        self._seed_packet(s3, {"1": ("FieldTicket", [0, 1]), "2": ("Invoice", [2, 3])})

        with pytest.raises(Exception, match="would no longer belong to any section"):
            test_set_index.update_test_set_document_sections(
                {
                    "input": {
                        "testSetId": "ts1",
                        "objectKey": "packet.pdf",
                        "sections": [{"sectionId": "1", "pageIndices": [0, 1, 2]}],
                    }
                }
            )

    def test_a_page_the_baseline_never_mentioned_is_allowed_in(self, labeling_env):
        """A split that dropped a page is exactly what a reviewer is here to fix."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        self._seed_packet(s3, {"1": ("FieldTicket", [0, 1])})

        test_set_index.update_test_set_document_sections(
            {
                "input": {
                    "testSetId": "ts1",
                    "objectKey": "packet.pdf",
                    # Page 2 was never in any section; the reviewer adds it.
                    "sections": [{"sectionId": "1", "pageIndices": [0, 1, 2]}],
                }
            }
        )

        assert self._read_sections(s3)["1"]["split_document"]["page_indices"] == [
            0,
            1,
            2,
        ]

    def test_an_empty_section_is_refused(self, labeling_env):
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        self._seed_packet(s3, {"1": ("FieldTicket", [0, 1])})

        with pytest.raises(Exception, match="has no pages"):
            test_set_index.update_test_set_document_sections(
                {
                    "input": {
                        "testSetId": "ts1",
                        "objectKey": "packet.pdf",
                        "sections": [
                            {"sectionId": "1", "pageIndices": [0, 1]},
                            {"sectionId": "2", "pageIndices": []},
                        ],
                    }
                }
            )

    def test_a_negative_or_non_integer_page_index_is_refused(self, labeling_env):
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        self._seed_packet(s3, {"1": ("FieldTicket", [0, 1])})

        for bad in (-1, "0", 1.5, True):
            with pytest.raises(Exception, match="invalid page index"):
                test_set_index.update_test_set_document_sections(
                    {
                        "input": {
                            "testSetId": "ts1",
                            "objectKey": "packet.pdf",
                            "sections": [
                                {"sectionId": "1", "pageIndices": [0, 1, bad]}
                            ],
                        }
                    }
                )

    def test_a_document_with_no_baseline_is_refused_with_advice(self, labeling_env):
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/packet.pdf", Body=b"x")

        with pytest.raises(Exception, match="Generate draft labels"):
            test_set_index.update_test_set_document_sections(
                {
                    "input": {
                        "testSetId": "ts1",
                        "objectKey": "packet.pdf",
                        "sections": [{"sectionId": "1", "pageIndices": [0]}],
                    }
                }
            )

    def test_a_document_class_is_validated_before_it_reaches_s3_metadata(self):
        """`documentClass` is Annotator-reachable and ends up as S3 user metadata.

        It flows reviewer -> reextractTestSetDocument -> generate_draft_labels ->
        SQS -> S3 object user metadata -> Document.from_s3_event -> a forced class
        and a forced section, and the dispatcher deliberately does not
        deep-validate nested input fields. S3 user metadata must be ASCII, so a
        non-ASCII value surfaced as a botocore failure and a 500 instead of a
        clean rejection.
        """
        v = test_set_index.validate_document_class

        # The real vocabulary, taken from shipped configs.
        # Real names, taken from config_library: all 120 shipped classes match this.
        for good in (
            "Bank Statement",
            "Bank-Statement",
            "Payslip",
            "invoice",
            "W-2",
            "BANK_CHECK",
            "PA-Claims-Evidence",
        ):
            assert v(good) is True, good

        # Absent means "leave the class alone", which is a legitimate call.
        assert v(None) is True
        assert v("") is True

        # Rejected.
        # `\s` would admit these, and `$` matches before a trailing newline.
        assert v("Bank\nStatement") is False
        assert v("Bank Statement\n") is False
        assert v("Bank\tStatement") is False
        assert v("Ünicode Class") is False  # not ASCII: botocore 500, not a 400
        assert v("x" * 101) is False
        # Caught a first draft of the regex that allowed '.' and '/'.
        assert v("../../etc/passwd") is False
        assert v("a/b") is False
        assert v({"not": "a string"}) is False
        assert v(123) is False

    def test_reextract_rejects_a_malformed_document_class(self, labeling_env):
        """The check runs before anything is written, not after."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)

        with pytest.raises(Exception, match="Invalid document class"):
            test_set_index.reextract_test_set_document(
                {
                    "input": {
                        "testSetId": "ts1",
                        "objectKey": "a.pdf",
                        "documentClass": "Ünicode",
                    }
                }
            )

    def test_clear_draft_labels_keeps_human_and_authored_ground_truth(
        self, labeling_env
    ):
        """Clearing drafts must never be a way to lose annotation work.

        Re-labeling with a corrected config is the normal tuning loop, so it must be
        safe to retry. Only labels explicitly tagged draft-machine are cleared —
        deliberately not "everything that isn't reviewed-human", because a baseline
        with no labelSource was supplied as ground truth when the set was created.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=3)
        cases = {
            "draft.pdf": {"labelSource": "draft-machine"},
            "reviewed.pdf": {"labelSource": "reviewed-human"},
            "authored.pdf": {},  # No labelSource: uploaded/generated ground truth.
        }
        for name, body in cases.items():
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/{name}/sections/1/result.json",
                Body=json.dumps({**body, "inference_result": {"f": "v"}}).encode(),
            )
        table.update_item(
            Key={"PK": "testset#ts1", "SK": "metadata"},
            UpdateExpression=(
                "SET labelJobId = :j, labelJobStatus = :s, labelProbedFileCount = :n"
            ),
            # Seeded, or the "marker is gone" assertion below passes against code that
            # never removes it.
            ExpressionAttributeValues={":j": "old-run", ":s": "COMPLETED", ":n": 3},
        )

        result = test_set_index.clear_draft_labels({"testSetId": "ts1"})

        surviving = {
            obj["Key"]
            for obj in s3.list_objects_v2(
                Bucket="test-set-bucket", Prefix="ts1/baseline/"
            ).get("Contents", [])
        }
        assert "ts1/baseline/draft.pdf/sections/1/result.json" not in surviving
        assert "ts1/baseline/reviewed.pdf/sections/1/result.json" in surviving
        assert "ts1/baseline/authored.pdf/sections/1/result.json" in surviving
        # The documents themselves stay — this clears labels, not the set.
        inputs = {
            obj["Key"]
            for obj in s3.list_objects_v2(
                Bucket="test-set-bucket", Prefix="ts1/input/"
            ).get("Contents", [])
        }
        assert len(inputs) == 3
        assert "1" in result["lastAddResult"]

        # The stale job pointer is dropped, or the set keeps reporting a run whose
        # output no longer exists.
        meta = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert "labelJobId" not in meta
        assert "labelJobStatus" not in meta
        # Confirmation is returned, not stored — see the reset test for why.
        assert "lastAddResult" not in meta
        # The probe marker must go, or _reconcile_label_state skips this set forever:
        # it is keyed on fileCount, and clearing drafts removes baselines without
        # changing membership. Observed on a dev stack as a cleared set permanently
        # reporting "Draft (machine)" and a 97.6% estimate with no labels present.
        assert "labelProbedFileCount" not in meta

    def test_clear_draft_labels_returns_a_wholly_drafted_set_to_unlabeled(
        self, labeling_env
    ):
        """Clearing every label must not leave the set claiming to have some.

        The label state is what drives the Labels badge and whether the review-effort
        estimator runs at all, so a set that reports "draft" with nothing under
        baseline/ also gets an estimated accuracy inferred entirely from a cross-set
        prior — a number about no labels.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=2)
        for name in ("a.pdf", "b.pdf"):
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/{name}/sections/1/result.json",
                Body=json.dumps(
                    {"labelSource": "draft-machine", "inference_result": {"f": "v"}}
                ).encode(),
            )
        table.update_item(
            Key={"PK": "testset#ts1", "SK": "metadata"},
            UpdateExpression="SET labelState = :d, labelProbedFileCount = :n",
            ExpressionAttributeValues={":d": "draft", ":n": 2},
        )

        test_set_index.clear_draft_labels({"testSetId": "ts1"})

        meta = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert meta["labelState"] == "unlabeled"

    def test_clear_draft_labels_leaves_state_to_the_reconciler_when_labels_survive(
        self, labeling_env
    ):
        """`kept` counts label objects, not documents, so it cannot decide the state.

        Some documents may have lost their only label while others keep theirs, which
        is "unlabeled" by the coverage rule registration uses — a question only
        _validate_test_set_files can answer. So the state is left alone and the probe
        marker dropped, which is what lets the reconciler answer it.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=2)
        for name, src in (("draft.pdf", "draft-machine"), ("kept.pdf", None)):
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
            body = {"inference_result": {"f": "v"}}
            if src:
                body["labelSource"] = src
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/{name}/sections/1/result.json",
                Body=json.dumps(body).encode(),
            )
        table.update_item(
            Key={"PK": "testset#ts1", "SK": "metadata"},
            UpdateExpression="SET labelState = :d, labelProbedFileCount = :n",
            ExpressionAttributeValues={":d": "draft", ":n": 2},
        )

        test_set_index.clear_draft_labels({"testSetId": "ts1"})

        meta = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        # Not asserted as "unlabeled": that would be guessing at coverage here.
        assert meta["labelState"] == "draft"
        # But the next list re-derives it, which is the whole point.
        assert "labelProbedFileCount" not in meta

    def test_reset_discards_reviewed_labels_and_review_state(self, labeling_env):
        """The destructive counterpart to clearDraftLabels.

        clearDraftLabels spares human work by design, so an annotated set cannot be
        returned to a clean state through it. Reset must remove the labels AND the
        review state that would otherwise leave the queue reporting the set fully
        reviewed with no labels present.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=2)
        for name, src in (("a.pdf", "reviewed-human"), ("b.pdf", "draft-machine")):
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/{name}/sections/1/result.json",
                Body=json.dumps({"labelSource": src}).encode(),
            )
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#run1",
                "jobId": "run1",
                "status": "COMPLETED",
                "objectKeys": ["a.pdf", "b.pdf"],
            }
        )
        table.put_item(Item={"PK": "testset#ts1", "SK": "curve#_aggregate", "n": 5})
        table.put_item(
            Item={
                "PK": "doc#run1/a.pdf",
                "SK": "none",
                "HITLStatus": "Completed",
                "HITLCompleted": True,
            }
        )

        result = test_set_index.reset_test_set_labels({"testSetId": "ts1"})

        # Every label is gone, reviewed included.
        assert not s3.list_objects_v2(
            Bucket="test-set-bucket", Prefix="ts1/baseline/"
        ).get("Contents")
        # Documents themselves survive — this resets labels, not membership.
        assert (
            len(
                s3.list_objects_v2(Bucket="test-set-bucket", Prefix="ts1/input/")[
                    "Contents"
                ]
            )
            == 2
        )
        # Review state cleared, or the queue still reads the set as reviewed.
        doc = table.get_item(Key={"PK": "doc#run1/a.pdf", "SK": "none"})["Item"]
        assert "HITLStatus" not in doc
        assert "HITLCompleted" not in doc
        # Label job and calibration history are gone.
        remaining = table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": "testset#ts1"},
        )["Items"]
        assert [i["SK"] for i in remaining] == ["metadata"]
        meta = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert meta["labelState"] == "unlabeled"
        assert "labelJobId" not in meta
        # Returned to the caller, which shows it as a transient confirmation...
        assert "Reset" in result["lastAddResult"]
        # ...but NOT persisted. Storing a confirmation on the record made it
        # immortal: the test-set list rendered it, dismissing only cleared client
        # state, the next poll re-read it, and nothing ever deleted it. No operation
        # persists one now — completions are announced transiently instead.
        assert "lastAddResult" not in meta

    def test_reset_discards_a_stale_notice_from_an_earlier_add(self, labeling_env):
        """Resetting invalidates whatever the last add reported."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        table.update_item(
            Key={"PK": "testset#ts1", "SK": "metadata"},
            UpdateExpression="SET lastAddResult = :r",
            ExpressionAttributeValues={":r": "Added 40 document(s)"},
        )

        test_set_index.reset_test_set_labels({"testSetId": "ts1"})

        meta = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert "lastAddResult" not in meta

    def test_reset_is_admin_only(self, labeling_env):
        """An Author manages test sets but must not be able to discard the team's
        annotation work."""
        table, _ = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        for group in ("Author", "Annotator", "Viewer"):
            with pytest.raises(Exception, match="Unauthorized"):
                test_set_index.handler(
                    {
                        "info": {"fieldName": "resetTestSetLabels"},
                        "identity": {"claims": {"cognito:groups": [group]}},
                        "arguments": {"testSetId": "ts1"},
                    },
                    None,
                )

    def test_queue_sorts_ground_truth_last_and_unlabeled_first(self, labeling_env):
        """Two kinds of "no confidence" must not sort the same.

        Ground truth was authored, not predicted: there is no self-assessment to be
        low and nothing for a reviewer to correct, so it belongs at the END. A
        document with no label at all belongs at the FRONT. Collapsing both to one
        sentinel points annotators at authored ground truth ahead of the genuinely
        uncertain drafts.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=3)
        for name in ("gt.pdf", "bare.pdf", "draft.pdf"):
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
        # Authored ground truth: no labelSource, no confidence.
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/gt.pdf/sections/1/result.json",
            Body=json.dumps({"inference_result": {"box_a": "authored"}}).encode(),
        )
        # A drafted document with real (mid) confidence.
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/draft.pdf/sections/1/result.json",
            Body=json.dumps(
                {
                    "labelSource": "draft-machine",
                    "explainability_info": [{"f": {"confidence": 0.5}}],
                    "inference_result": {"f": "v"},
                }
            ).encode(),
        )
        # bare.pdf has no baseline at all.

        result = test_set_index.get_annotation_queue({"testSetId": "ts1"}, None)
        order = [d["objectKey"] for d in result["documents"]]
        assert order == ["bare.pdf", "draft.pdf", "gt.pdf"], order
        assert result["nextObjectKey"] == "bare.pdf"

    def test_queue_orders_by_alert_count_not_lowest_confidence(self, labeling_env):
        """Review work is the number of fields to check, not the worst score.

        many.pdf has three fields below their threshold; one.pdf has a single weaker
        field. Ordering by minConfidence puts one.pdf first even though many.pdf is
        three times the work, so the queue counts alerts and uses confidence only to
        break ties.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=2)
        for name in ("many.pdf", "one.pdf"):
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/many.pdf/sections/1/result.json",
            Body=json.dumps(
                {
                    "labelSource": "draft-machine",
                    "explainability_info": [
                        {
                            "a": {"confidence": 0.5, "confidence_threshold": 0.9},
                            "b": {"confidence": 0.6, "confidence_threshold": 0.9},
                            "c": {"confidence": 0.7, "confidence_threshold": 0.9},
                        }
                    ],
                    "inference_result": {"a": "1", "b": "2", "c": "3"},
                }
            ).encode(),
        )
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/one.pdf/sections/1/result.json",
            Body=json.dumps(
                {
                    "labelSource": "draft-machine",
                    "explainability_info": [
                        {"a": {"confidence": 0.2, "confidence_threshold": 0.9}}
                    ],
                    "inference_result": {"a": "1"},
                }
            ).encode(),
        )

        result = test_set_index.get_annotation_queue({"testSetId": "ts1"}, None)
        by_key = {d["objectKey"]: d for d in result["documents"]}
        assert by_key["many.pdf"]["alertCount"] == 3
        assert by_key["many.pdf"]["fieldCount"] == 3
        assert by_key["one.pdf"]["alertCount"] == 1
        assert [d["objectKey"] for d in result["documents"]] == ["many.pdf", "one.pdf"]

    def test_estimate_excludes_ground_truth_from_reviewable_work(self, labeling_env):
        """Reviewing authored labels is not work the estimate should ask for."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=2)
        for name in ("gt.pdf", "draft.pdf"):
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/gt.pdf/sections/1/result.json",
            Body=json.dumps({"inference_result": {"box_a": "authored"}}).encode(),
        )
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/draft.pdf/sections/1/result.json",
            Body=json.dumps(
                {
                    "labelSource": "draft-machine",
                    "explainability_info": [{"f": {"confidence": 0.5}}],
                    "inference_result": {"f": "v"},
                }
            ).encode(),
        )

        result = test_set_index.estimate_review_effort({"testSetId": "ts1"})
        # One of the two documents is authored ground truth, so only one is
        # reviewable — reporting 2 would bill the owner for finished work.
        assert result["totalDocs"] == 1
        assert result["docsToReview"] <= 1

    def test_reharvest_prunes_sections_the_new_run_no_longer_produces(
        self, labeling_env
    ):
        """Regression: orphan sections from an earlier run must not mask a fix.

        A document's confidence is the minimum across its sections, so a stale
        low-scoring section hides a corrected run that scored every real field well.
        Orphans also linger in the annotation queue as sections of a document that no
        longer has them.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        # A previous run left 3 draft sections behind.
        for n in (1, 2, 3):
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/a.pdf/sections/{n}/result.json",
                Body=json.dumps(
                    {
                        "labelSource": "draft-machine",
                        "inference_result": {"f": f"old-{n}"},
                        "explainability_info": [{"f": {"confidence": 0.5}}],
                    }
                ).encode(),
            )
        # The new run produces only section 1.
        uri = _seed_pipeline_result(
            s3, "run2/a.pdf/sections/1/result.json", {"f": "new"}
        )
        _seed_completed_run(
            table,
            "run2",
            "ts1",
            ["a.pdf"],
            {"a.pdf": [{"Id": "1", "OutputJSONUri": uri}]},
        )
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#run2",
                "testSetId": "ts1",
                "jobId": "run2",
                "status": "RUNNING",
                "total": 1,
                "labeled": 0,
            }
        )

        test_set_index.get_draft_label_job({"testSetId": "ts1", "jobId": "run2"})

        keys = [
            o["Key"]
            for o in s3.list_objects_v2(
                Bucket="test-set-bucket", Prefix="ts1/baseline/a.pdf/sections/"
            ).get("Contents", [])
        ]
        assert keys == ["ts1/baseline/a.pdf/sections/1/result.json"], keys
        body = json.loads(
            s3.get_object(Bucket="test-set-bucket", Key=keys[0])["Body"].read()
        )
        assert body["inference_result"] == {"f": "new"}

    def test_pruning_never_touches_ground_truth_or_reviewed_labels(self, labeling_env):
        """The destructive path must only ever remove disposable machine drafts."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        # Section 2 is authored ground truth (no labelSource); 3 is human-reviewed.
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/2/result.json",
            Body=json.dumps({"inference_result": {"box_a": "authored"}}).encode(),
        )
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/3/result.json",
            Body=json.dumps(
                {"labelSource": "reviewed-human", "inference_result": {"f": "checked"}}
            ).encode(),
        )
        uri = _seed_pipeline_result(
            s3, "run2/a.pdf/sections/1/result.json", {"f": "new"}
        )
        _seed_completed_run(
            table,
            "run2",
            "ts1",
            ["a.pdf"],
            {"a.pdf": [{"Id": "1", "OutputJSONUri": uri}]},
        )
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#run2",
                "testSetId": "ts1",
                "jobId": "run2",
                "status": "RUNNING",
                "total": 1,
                "labeled": 0,
            }
        )

        test_set_index.get_draft_label_job({"testSetId": "ts1", "jobId": "run2"})

        keys = sorted(
            o["Key"]
            for o in s3.list_objects_v2(
                Bucket="test-set-bucket", Prefix="ts1/baseline/a.pdf/sections/"
            ).get("Contents", [])
        )
        # All three survive: the two protected ones were never eligible.
        assert keys == [
            "ts1/baseline/a.pdf/sections/1/result.json",
            "ts1/baseline/a.pdf/sections/2/result.json",
            "ts1/baseline/a.pdf/sections/3/result.json",
        ], keys

    def test_pruning_does_not_run_when_the_harvest_wrote_nothing(self, labeling_env):
        """A run that harvests nothing must not empty an existing baseline.

        Otherwise a partial pipeline failure would delete the previous run's
        perfectly good drafts and leave the document with no labels at all.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=json.dumps(
                {"labelSource": "draft-machine", "inference_result": {"f": "keep"}}
            ).encode(),
        )
        # Run finished but produced no usable section output.
        _seed_completed_run(table, "run2", "ts1", ["a.pdf"], {"a.pdf": []})
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#run2",
                "testSetId": "ts1",
                "jobId": "run2",
                "status": "RUNNING",
                "total": 1,
                "labeled": 0,
            }
        )

        test_set_index.get_draft_label_job({"testSetId": "ts1", "jobId": "run2"})

        body = json.loads(
            s3.get_object(
                Bucket="test-set-bucket",
                Key="ts1/baseline/a.pdf/sections/1/result.json",
            )["Body"].read()
        )
        assert body["inference_result"] == {"f": "keep"}

    def test_documents_page_surfaces_a_running_job_for_rehydration(self, labeling_env):
        """A page load must be able to resume polling a job it did not start."""
        table, s3 = labeling_env
        _seed_test_set(
            table, "ts1", fileCount=1, labelJobId="run9", labelJobStatus="RUNNING"
        )
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")

        page = test_set_index.get_test_set_documents({"testSetId": "ts1"})
        assert page["activeLabelJobId"] == "run9"

    def test_the_annotation_queue_carries_the_class_through(self, labeling_env):
        """End to end: the queue is a different resolver from the documents page,
        so the field has to survive that hop too. Pinned because the value is
        only useful where the review work actually happens."""
        table, s3 = labeling_env
        _seed_test_set(
            table, "ts1", fileCount=1, labelJobId="run1", labelJobStatus="COMPLETED"
        )
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=json.dumps(
                {
                    "labelSource": "draft-machine",
                    "document_class": {"type": "Bank-Statement"},
                    "inference_result": {"f": "v"},
                }
            ).encode(),
        )

        queue = test_set_index.get_annotation_queue({"testSetId": "ts1"})

        assert queue["documents"][0]["documentClasses"] == ["Bank-Statement"]

    def test_queue_shows_what_each_document_was_classified_as(self, labeling_env):
        """A reviewer must be able to see the class without opening the document.

        It is the one thing no other column can reveal. Extraction against the
        wrong schema can be *confidently* wrong, so a misclassified document's
        confidence and alert count look entirely normal — which is why it sorts
        low in worst-first order and never gets opened.

        Shown, not scored: nothing here can tell whether the class is WRONG,
        because the draft under review is itself the candidate ground truth and
        classification carries no real confidence. So it is deliberately kept out
        of the ordering and out of the review-effort estimator.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=json.dumps(
                {
                    "labelSource": "draft-machine",
                    "document_class": {"type": "Bank-Statement"},
                    "inference_result": {"f": "v"},
                }
            ).encode(),
        )

        page = test_set_index.get_test_set_documents({"testSetId": "ts1"})

        assert page["documents"][0]["documentClasses"] == ["Bank-Statement"]

    def test_each_section_reports_its_page_grouping(self, labeling_env):
        """The page-regrouping editor needs every section's grouping at once.

        Without this it would have to fetch each section's result.json again — the
        editor otherwise loads only the section being viewed. The resolver is already
        opening these files for label state and class, so this costs no extra reads.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/packet.pdf", Body=b"x")
        for section, cls, indices in (("1", "Invoice", [0, 1]), ("2", "W2", [2])):
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/packet.pdf/sections/{section}/result.json",
                Body=json.dumps(
                    {
                        "labelSource": "draft-machine",
                        "document_class": {"type": cls},
                        "split_document": {"page_indices": indices},
                        "inference_result": {"f": "v"},
                    }
                ).encode(),
            )

        page = test_set_index.get_test_set_documents({"testSetId": "ts1"})
        sections = page["documents"][0]["sections"]

        assert [s["sectionId"] for s in sections] == ["1", "2"]
        assert [s["pageIndices"] for s in sections] == [[0, 1], [2]]

    def test_a_section_with_no_grouping_omits_it_rather_than_claiming_no_pages(
        self, labeling_env
    ):
        """Absent and empty are different claims.

        A non-packet baseline carries no split_document at all. Reporting [] would say
        "this section covers no pages", which would make the regrouping editor show it
        as empty and refuse to save.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=json.dumps(
                {
                    "labelSource": "draft-machine",
                    "document_class": {"type": "Bank-Statement"},
                    "inference_result": {"f": "v"},
                }
            ).encode(),
        )

        page = test_set_index.get_test_set_documents({"testSetId": "ts1"})

        assert "pageIndices" not in page["documents"][0]["sections"][0]

    def test_the_queue_reports_page_groupings_too(self, labeling_env):
        """Both payloads, because the editor is reached from either."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/packet.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/packet.pdf/sections/1/result.json",
            Body=json.dumps(
                {
                    "labelSource": "draft-machine",
                    "document_class": {"type": "Invoice"},
                    "split_document": {"page_indices": [0, 1, 2]},
                    "inference_result": {"f": "v"},
                }
            ).encode(),
        )

        queue = test_set_index.get_annotation_queue({"testSetId": "ts1"})

        assert queue["documents"][0]["sections"][0]["pageIndices"] == [0, 1, 2]

    def test_a_packet_reports_each_distinct_class_once(self, labeling_env):
        """A split document has a class per section. Duplicates collapse so a
        20-page packet of one class does not render twenty identical badges."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        for section, cls in (("1", "Invoice"), ("2", "W2"), ("3", "Invoice")):
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/a.pdf/sections/{section}/result.json",
                Body=json.dumps(
                    {
                        "labelSource": "draft-machine",
                        "document_class": {"type": cls},
                        "inference_result": {"f": "v"},
                    }
                ).encode(),
            )

        page = test_set_index.get_test_set_documents({"testSetId": "ts1"})

        # Distinct, and in the order encountered.
        assert page["documents"][0]["documentClasses"] == ["Invoice", "W2"]

    def test_a_baseline_with_no_class_reports_none_rather_than_a_blank(
        self, labeling_env
    ):
        """An empty list, not [""] — a blank badge would read as a class named ""."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=json.dumps(
                {"labelSource": "draft-machine", "inference_result": {"f": "v"}}
            ).encode(),
        )

        page = test_set_index.get_test_set_documents({"testSetId": "ts1"})

        assert page["documents"][0]["documentClasses"] == []

    def test_documents_page_reports_the_SET_size_not_the_page_size(self, labeling_env):
        """A paginated response must say how big the whole set is.

        Without it a caller can only count what it received and call that the
        total, which is what happened: the UI showed "Documents (50)" for a
        100-document set and offered to "Label 50 document(s)" — then labeled all
        100, because select-all sends no object keys and the server walks the set
        itself. The number shown was wrong in the one direction that matters.

        Read from the stored fileCount, so it stays O(1) as sets grow.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=100)
        for i in range(3):
            s3.put_object(
                Bucket="test-set-bucket", Key=f"ts1/input/doc{i}.pdf", Body=b"x"
            )

        page = test_set_index.get_test_set_documents({"testSetId": "ts1", "limit": 2})

        assert len(page["documents"]) == 2, "page is capped by limit"
        assert page["totalCount"] == 100, "but the total describes the whole set"
        assert page["nextToken"], "and there is more to fetch"

    def test_total_count_is_zero_rather_than_absent_when_unknown(self, labeling_env):
        """A missing fileCount must not surface as null and render as blank."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1")
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")

        page = test_set_index.get_test_set_documents({"testSetId": "ts1"})

        assert page["totalCount"] == 0

    def test_documents_page_omits_the_job_once_it_is_finished(self, labeling_env):
        table, s3 = labeling_env
        _seed_test_set(
            table, "ts1", fileCount=1, labelJobId="run9", labelJobStatus="COMPLETED"
        )
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")

        page = test_set_index.get_test_set_documents({"testSetId": "ts1"})
        assert "activeLabelJobId" not in page

    def test_queue_gives_no_review_key_to_documents_the_run_skipped(self, labeling_env):
        """Regression: no review key for a document with no pipeline copy.

        Draft labeling skips documents that already carry ground truth, so no
        pipeline copy exists for them. Handing out a review key for every document
        whenever the set has *any* labeling run makes claiming such a document fail
        with "Document <runId>/<file> not found".
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=2, labelJobId="run1")
        for name in ("drafted.pdf", "gt.pdf"):
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/gt.pdf/sections/1/result.json",
            Body=json.dumps({"inference_result": {"box_a": "authored"}}).encode(),
        )
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/drafted.pdf/sections/1/result.json",
            Body=json.dumps(
                {
                    "labelSource": "draft-machine",
                    "inference_result": {"f": "v"},
                    "explainability_info": [{"f": {"confidence": 0.5}}],
                }
            ).encode(),
        )
        # Only the drafted document has a pipeline copy from the run.
        table.put_item(
            Item={
                "PK": "doc#run1/drafted.pdf",
                "SK": "none",
                "ObjectStatus": "COMPLETED",
            }
        )

        result = test_set_index.get_annotation_queue({"testSetId": "ts1"}, None)
        by_key = {d["objectKey"]: d for d in result["documents"]}
        assert by_key["drafted.pdf"]["reviewObjectKey"] == "run1/drafted.pdf"
        assert by_key["gt.pdf"]["reviewObjectKey"] is None

    def test_harvest_records_the_config_that_produced_the_labels(self, labeling_env):
        """completeSectionReview keys the confidence curve on this.

        It reads metadata.config_version off the baseline to decide which curve a
        review observation belongs to. If the harvester omits that field, reviews
        land in the version-agnostic _aggregate curve while scoring runs write to the
        per-version one, and the two halves of the calibration signal never combine.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        uri = _seed_pipeline_result(
            s3, "ts1-run/a.pdf/sections/1/result.json", {"vendor": "Acme"}
        )
        _seed_completed_run(
            table,
            "ts1-run",
            "ts1",
            ["a.pdf"],
            {"a.pdf": [{"Id": "1", "OutputJSONUri": uri}]},
        )
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#ts1-run",
                "testSetId": "ts1",
                "jobId": "ts1-run",
                "status": "RUNNING",
                "configVersion": "my-config-v2",
                "total": 1,
                "labeled": 0,
            }
        )

        test_set_index.get_draft_label_job({"testSetId": "ts1", "jobId": "ts1-run"})

        body = json.loads(
            s3.get_object(
                Bucket="test-set-bucket",
                Key="ts1/baseline/a.pdf/sections/1/result.json",
            )["Body"].read()
        )
        assert body["metadata"]["config_version"] == "my-config-v2"

    def test_harvest_preserves_a_config_version_already_in_metadata(self, labeling_env):
        """The pipeline's own value wins — setdefault, not overwrite."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        s3.put_object(
            Bucket="output-bucket",
            Key="ts1-run/a.pdf/sections/1/result.json",
            Body=json.dumps(
                {
                    "inference_result": {"vendor": "Acme"},
                    "metadata": {"config_version": "from-pipeline"},
                }
            ).encode(),
        )
        _seed_completed_run(
            table,
            "ts1-run",
            "ts1",
            ["a.pdf"],
            {
                "a.pdf": [
                    {
                        "Id": "1",
                        "OutputJSONUri": "s3://output-bucket/ts1-run/a.pdf/sections/1/result.json",
                    }
                ]
            },
        )
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#ts1-run",
                "testSetId": "ts1",
                "jobId": "ts1-run",
                "status": "RUNNING",
                "configVersion": "from-job",
                "total": 1,
                "labeled": 0,
            }
        )

        test_set_index.get_draft_label_job({"testSetId": "ts1", "jobId": "ts1-run"})

        body = json.loads(
            s3.get_object(
                Bucket="test-set-bucket",
                Key="ts1/baseline/a.pdf/sections/1/result.json",
            )["Body"].read()
        )
        assert body["metadata"]["config_version"] == "from-pipeline"

    def test_queue_harvests_the_running_label_job(self, labeling_env):
        """Regression: the queue must itself advance draft labeling.

        Labels are harvested on read, so whoever polls drives the harvest. If only
        the owner-facing detail page polls — a page an Annotator cannot open — an
        annotator opening the workspace mid-run watches an empty queue that never
        fills, with the job frozen at 0 labeled.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1, labelJobId="ts1-run")
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        uri = _seed_pipeline_result(
            s3,
            "ts1-run/a.pdf/sections/1/result.json",
            {"vendor": "Acme"},
            [{"vendor": {"confidence": 0.42, "confidence_threshold": 0.8}}],
        )
        _seed_completed_run(
            table,
            "ts1-run",
            "ts1",
            ["a.pdf"],
            {"a.pdf": [{"Id": "1", "OutputJSONUri": uri}]},
        )
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#ts1-run",
                "testSetId": "ts1",
                "jobId": "ts1-run",
                "status": "RUNNING",
                "total": 1,
                "labeled": 0,
            }
        )

        result = test_set_index.get_annotation_queue({"testSetId": "ts1"}, None)

        assert result["labelJobStatus"] == "COMPLETED"
        assert result["labelJobLabeled"] == 1
        assert result["labelJobTotal"] == 1
        body = json.loads(
            s3.get_object(
                Bucket="test-set-bucket",
                Key="ts1/baseline/a.pdf/sections/1/result.json",
            )["Body"].read()
        )
        assert body["labelSource"] == "draft-machine"

    def test_queue_reports_a_still_running_label_job(self, labeling_env):
        """An empty queue must distinguish "still labeling" from "nothing to do".

        Without this the workspace showed "Queue complete — every document has
        been reviewed" while labeling was still producing the documents.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=2, labelJobId="ts1-run")
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        _seed_completed_run(table, "ts1-run", "ts1", ["a.pdf", "pending.pdf"], {})
        table.put_item(
            Item={
                "PK": "doc#ts1-run/pending.pdf",
                "SK": "none",
                "ObjectStatus": "RUNNING",
                "Sections": [],
            }
        )
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#ts1-run",
                "testSetId": "ts1",
                "jobId": "ts1-run",
                "status": "RUNNING",
                "total": 2,
                "labeled": 0,
            }
        )

        result = test_set_index.get_annotation_queue({"testSetId": "ts1"}, None)
        assert result["labelJobStatus"] == "RUNNING"
        assert result["labelJobTotal"] == 2

    def test_queue_survives_a_failing_harvest(self, labeling_env):
        """A harvest failure must not take down the queue.

        Documents already labeled are still reviewable, so degrade to serving
        them rather than failing the whole page.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1, labelJobId="ts1-run")
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#ts1-run",
                "testSetId": "ts1",
                "jobId": "ts1-run",
                "status": "RUNNING",
                "total": 1,
                "labeled": 0,
            }
        )

        with patch.object(
            test_set_index, "_harvest_label_job", side_effect=RuntimeError("boom")
        ):
            result = test_set_index.get_annotation_queue({"testSetId": "ts1"}, None)

        assert result["documents"][0]["objectKey"] == "a.pdf"
        assert result["labelJobStatus"] == "RUNNING"

    def test_queue_reports_no_job_fields_when_none_ran(self, labeling_env):
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")

        result = test_set_index.get_annotation_queue({"testSetId": "ts1"}, None)
        assert result["labelJobStatus"] is None
        assert result["labelJobLabeled"] is None
        assert result["labelJobTotal"] is None

    def test_harvest_stamps_the_test_set_onto_the_pipeline_document(self, labeling_env):
        """Regression: without TestSetId, a reviewer's save silently loses everything.

        write_correction_to_test_set_baseline keys on the doc item's TestSetId to find
        the owning set. Draft labeling does not go through sendTestRunToReview, so if
        it omits TestSetId the write-back, the reviewed-human tag and the
        confidence-curve observation are all skipped while completeSectionReview
        still reports success.
        """
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        uri = _seed_pipeline_result(
            s3, "ts1-run/a.pdf/sections/1/result.json", {"vendor": "Acme"}
        )
        _seed_completed_run(
            table,
            "ts1-run",
            "ts1",
            ["a.pdf"],
            {"a.pdf": [{"Id": "1", "OutputJSONUri": uri}]},
        )
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#ts1-run",
                "testSetId": "ts1",
                "jobId": "ts1-run",
                "status": "RUNNING",
                "total": 1,
                "labeled": 0,
            }
        )
        # Precondition: the pipeline doc has no TestSetId yet.
        doc_before = table.get_item(Key={"PK": "doc#ts1-run/a.pdf", "SK": "none"})[
            "Item"
        ]
        assert "TestSetId" not in doc_before

        test_set_index.get_draft_label_job({"testSetId": "ts1", "jobId": "ts1-run"})

        doc_after = table.get_item(Key={"PK": "doc#ts1-run/a.pdf", "SK": "none"})[
            "Item"
        ]
        assert doc_after["TestSetId"] == "ts1"

    def test_harvest_does_not_overwrite_an_existing_test_set_stamp(self, labeling_env):
        """A doc already routed via sendTestRunToReview keeps its attribution."""
        table, s3 = labeling_env
        _seed_test_set(table, "ts1", fileCount=1)
        uri = _seed_pipeline_result(
            s3, "ts1-run/a.pdf/sections/1/result.json", {"vendor": "Acme"}
        )
        _seed_completed_run(
            table,
            "ts1-run",
            "ts1",
            ["a.pdf"],
            {"a.pdf": [{"Id": "1", "OutputJSONUri": uri}]},
        )
        table.update_item(
            Key={"PK": "doc#ts1-run/a.pdf", "SK": "none"},
            UpdateExpression="SET TestSetId = :t",
            ExpressionAttributeValues={":t": "original-set"},
        )
        table.put_item(
            Item={
                "PK": "testset#ts1",
                "SK": "labeljob#ts1-run",
                "testSetId": "ts1",
                "jobId": "ts1-run",
                "status": "RUNNING",
                "total": 1,
                "labeled": 0,
            }
        )

        test_set_index.get_draft_label_job({"testSetId": "ts1", "jobId": "ts1-run"})

        doc = table.get_item(Key={"PK": "doc#ts1-run/a.pdf", "SK": "none"})["Item"]
        assert doc["TestSetId"] == "original-set"

    # -- Reconcile existing folders on direct-S3 edits --------------------
    #
    # Manual S3 additions to <prefix>/input/ or <prefix>/baseline/ don't fire
    # any event notification, so the ``getTestSets`` lazy scan is the only place
    # fileCount / status can be refreshed. Before this reconcile path existed,
    # the ``if prefix in existing_test_sets: continue`` short-circuit left rows
    # stale forever.

    def test_validator_returns_signature_of_current_state(self):
        """The signature must move whenever input or baseline listings change.

        The reconcile fast-path relies on this: identical signature means no
        DDB write. If the validator ever returns a constant signature, every
        reconcile would over-write (or never over-write) the row.
        """
        from datetime import datetime, timezone

        s3 = Mock()
        s3.get_paginator.return_value.paginate.side_effect = lambda **kw: (
            [
                {
                    "Contents": [
                        {
                            "Key": "ts1/input/a.pdf",
                            "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc),
                        }
                    ]
                }
            ]
            if "input" in kw["Prefix"]
            else [
                {
                    "Contents": [
                        {
                            "Key": "ts1/baseline/a.pdf/sections/1/result.json",
                            "LastModified": datetime(2026, 1, 2, tzinfo=timezone.utc),
                        }
                    ]
                }
            ]
        )
        result = test_set_index._validate_test_set_files(s3, "bucket", "ts1")
        assert result["signature"] == "1:1:2026-01-02T00:00:00+00:00"

    def test_reconcile_noop_when_signature_matches(self):
        """Fast path: no DDB write when the folder is unchanged.

        The signature is the combined base + source-marker form the reconcile
        actually writes (``…|src=uploaded``), so a matching prior signature
        short-circuits.
        """
        s3 = Mock()
        existing = {
            "PK": "testset#ts1",
            "SK": "metadata",
            "status": "COMPLETED",
            "fileCount": 3,
            "labelState": "labeled",
            "source": "uploaded",
            "contentSignature": "3:3:2026-01-02T00:00:00+00:00|src=uploaded",
        }
        with (
            patch.object(
                test_set_index,
                "_validate_test_set_files",
                return_value={
                    "valid": True,
                    "input_count": 3,
                    "labeled": True,
                    "signature": "3:3:2026-01-02T00:00:00+00:00",
                },
            ),
            patch.object(
                test_set_index, "_get_test_set_source", return_value="uploaded"
            ),
        ):
            result = test_set_index._reconcile_test_set_tracking_entry(
                s3, "bucket", "ts1", existing
            )

        assert result is None

    def test_reconcile_softens_partial_pairing_on_labeled_row(self, labeling_env):
        """A labeled row with partial pairing MUST NOT be flagged as FAILED.

        The draft-labeling harvester writes baselines one document per poll,
        and ``clear_draft_labels`` deliberately keeps human labels while
        dropping drafts — both leave partial pairing as a legitimate
        transient/steady state. Applying the new-folder hard-fail rule to
        an already-labeled row would break every draft-labeling run and
        misreport ``clear_draft_labels``'s post-state as broken.

        Reconcile refreshes ``fileCount`` (so the count-drift bug this MR
        was written to fix is still repaired) and updates the signature,
        but leaves ``status`` / ``labelState`` / ``error`` alone.
        """
        table, s3 = labeling_env

        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/b.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=b"{}",
        )
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/b.pdf/sections/1/result.json",
            Body=b"{}",
        )
        _seed_test_set(
            table,
            "ts1",
            name="ts1",
            status="COMPLETED",
            fileCount=2,
            labelState="labeled",
            source="uploaded",
            createdAt="2026-01-01T00:00:00Z",
            InitialEventTime="2026-01-01T00:00:00Z",
            ItemType="testset",
            contentSignature="stale",
        )

        # User drops a third input with no matching baseline — the exact
        # partial-pairing shape that was previously flagged as FAILED.
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/c.pdf", Body=b"x")

        existing_row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})[
            "Item"
        ]
        result = test_set_index._reconcile_test_set_tracking_entry(
            s3, "test-set-bucket", "ts1", existing_row
        )

        assert result is not None
        # fileCount tracks the S3 truth (the whole point of the MR).
        assert result["fileCount"] == 3
        # status / labelState / error preserved — this is the labeling
        # lifecycle protection.
        assert result["status"] == "COMPLETED"
        assert result["labelState"] == "labeled"
        assert result.get("error") is None

        row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert row["fileCount"] == 3
        assert row["status"] == "COMPLETED"
        assert row["labelState"] == "labeled"
        assert "error" not in row
        assert row["createdAt"] == "2026-01-01T00:00:00Z"
        assert "updatedAt" in row

    def test_reconcile_never_hard_fails_partial_pairing_on_registered_row(
        self, labeling_env
    ):
        """Round-4 finding: partial pairing on an already-registered row is
        NEVER the "botched upload" signal.

        The reviewer's probe: a labelled set has an input added via direct
        S3, ``_reconcile_label_state`` (which runs earlier in the same
        ``get_test_sets`` call) demotes labelState to 'unlabeled' in place,
        and my previous softening — which keyed on
        ``labelState in ('draft','labeled')`` — was defeated because the
        in-memory row it inspected had already been demoted. Result was a
        red FAILED banner on a set that is simply mid-labelling.

        Fixed by keying the softening on the validator's error class
        (``Missing baseline files for:...`` / ``Extra baseline files:...``),
        which looks at S3 rather than at the (demote-able) labelState. This
        test exercises the specific labelState=='unlabeled' shape that used
        to hard-fail: no matter how the demotion happened, reconcile no
        longer flips status to FAILED for partial pairing on a row that
        exists.
        """
        table, s3 = labeling_env

        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        _seed_test_set(
            table,
            "ts1",
            name="ts1",
            status="COMPLETED",
            fileCount=1,
            labelState="unlabeled",  # e.g. after demotion by _reconcile_label_state
            source="uploaded",
            createdAt="2026-01-01T00:00:00Z",
            InitialEventTime="2026-01-01T00:00:00Z",
            ItemType="testset",
            contentSignature="stale",
        )

        # Add another input; add a baseline for it but NOT for a.pdf.
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/b.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/b.pdf/sections/1/result.json",
            Body=b"{}",
        )

        existing_row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})[
            "Item"
        ]
        result = test_set_index._reconcile_test_set_tracking_entry(
            s3, "test-set-bucket", "ts1", existing_row
        )
        # No FAILED, no error write — status and error stay put; only the
        # observable delta (fileCount) is refreshed.
        assert result is not None
        assert result["status"] == "COMPLETED"
        assert result.get("error") is None
        assert result["fileCount"] == 2
        row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert row["status"] == "COMPLETED"
        assert row["fileCount"] == 2
        assert "error" not in row

    def test_reconcile_hard_fails_when_all_inputs_deleted(self, labeling_env):
        """The one hard-fail condition reconcile still emits.

        ``No input files found`` is genuinely broken — every input file has
        been deleted from S3 while the row survives. Operator should see
        FAILED so the state doesn't hide.
        """
        table, s3 = labeling_env

        # No input files in S3 at all.
        _seed_test_set(
            table,
            "ts1",
            name="ts1",
            status="COMPLETED",
            fileCount=1,
            labelState="labeled",
            source="uploaded",
            createdAt="2026-01-01T00:00:00Z",
            InitialEventTime="2026-01-01T00:00:00Z",
            ItemType="testset",
            contentSignature="stale",
        )

        existing_row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})[
            "Item"
        ]
        result = test_set_index._reconcile_test_set_tracking_entry(
            s3, "test-set-bucket", "ts1", existing_row
        )
        assert result is not None
        assert result["status"] == "FAILED"
        assert result["error"] == "No input files found"

    def test_reconcile_clears_error_when_baseline_added_back(self, labeling_env):
        """A row FAILED yesterday must recover when the missing baseline arrives."""
        table, s3 = labeling_env

        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=b"{}",
        )
        _seed_test_set(
            table,
            "ts1",
            name="ts1",
            status="FAILED",
            fileCount=1,
            labelState="unlabeled",
            source="uploaded",
            createdAt="2026-01-01T00:00:00Z",
            InitialEventTime="2026-01-01T00:00:00Z",
            ItemType="testset",
            error="Missing baseline files for: a.pdf",
            contentSignature="stale",
        )

        existing_row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})[
            "Item"
        ]
        result = test_set_index._reconcile_test_set_tracking_entry(
            s3, "test-set-bucket", "ts1", existing_row
        )

        assert result is not None
        assert result["status"] == "COMPLETED"
        # The reconcile helper strips the error from the in-memory dict when
        # DDB REMOVEs it, so the resolver's returned entry is clean.
        assert "error" not in result

        row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert row["status"] == "COMPLETED"
        assert "error" not in row

    def test_reconcile_skips_row_in_non_terminal_status(self):
        """UPDATING/PROCESSING rows must not be reconciled — a copier is mid-write."""
        s3 = Mock()
        with (
            patch.object(test_set_index, "_validate_test_set_files") as validate,
            patch.object(test_set_index.db_client, "update_item") as upd,
        ):
            result = test_set_index._reconcile_test_set_tracking_entry(
                s3,
                "bucket",
                "ts1",
                {"PK": "testset#ts1", "SK": "metadata", "status": "UPDATING"},
            )
        assert result is None
        validate.assert_not_called()
        upd.assert_not_called()

    def test_reconcile_first_run_backfills_signature(self, labeling_env):
        """First reconcile after upgrade writes a signature onto a row that lacks one.

        Rows written before the reconcile logic existed have no
        ``contentSignature`` attribute; without a one-time write to backfill
        it, the fast path would never engage for those rows and every poll
        would incur the full validate + DDB write.
        """
        table, s3 = labeling_env

        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=b"{}",
        )
        _seed_test_set(
            table,
            "ts1",
            name="ts1",
            status="COMPLETED",
            fileCount=1,
            labelState="labeled",
            source="uploaded",
            createdAt="2026-01-01T00:00:00Z",
            InitialEventTime="2026-01-01T00:00:00Z",
            ItemType="testset",
            # No contentSignature yet — row predates the reconcile logic.
        )

        existing_row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})[
            "Item"
        ]
        result = test_set_index._reconcile_test_set_tracking_entry(
            s3, "test-set-bucket", "ts1", existing_row
        )

        assert result is not None
        assert "contentSignature" in result
        row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert row.get("contentSignature")

    def test_reconcile_updates_file_count_when_files_added(self, labeling_env):
        """Happy path: user adds a matched pair, count and signature move."""
        table, s3 = labeling_env

        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=b"{}",
        )
        _seed_test_set(
            table,
            "ts1",
            name="ts1",
            status="COMPLETED",
            fileCount=1,
            labelState="labeled",
            source="uploaded",
            createdAt="2026-01-01T00:00:00Z",
            InitialEventTime="2026-01-01T00:00:00Z",
            ItemType="testset",
            contentSignature="stale",
        )

        # Add a matched pair.
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/b.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/b.pdf/sections/1/result.json",
            Body=b"{}",
        )

        existing_row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})[
            "Item"
        ]
        result = test_set_index._reconcile_test_set_tracking_entry(
            s3, "test-set-bucket", "ts1", existing_row
        )

        assert result is not None
        assert result["fileCount"] == 2
        assert result["status"] == "COMPLETED"
        row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert row["fileCount"] == 2
        assert row["contentSignature"] != "stale"

    # -- Adversarial edge cases from the code-review pass -----------------

    def test_reconcile_skips_row_when_validator_transiently_fails(self):
        """A transient S3 exception in the validator must NOT clobber a valid row.

        Without this guard, a throttled ``list_objects_v2`` returns
        ``input_count=0, valid=False, labeled=False`` — reconcile would write
        those over the row and destroy its fileCount/status/labelState. The
        validator flags the failure with ``validation_failed=True`` for exactly
        this case; reconcile bails.
        """
        s3 = Mock()
        with patch.object(
            test_set_index,
            "_validate_test_set_files",
            return_value={
                "valid": False,
                "validation_failed": True,
                "error": "Validation error: Throttled",
                "input_count": 0,
                "labeled": False,
                "signature": "",
            },
        ):
            existing = {
                "PK": "testset#ts1",
                "SK": "metadata",
                "status": "COMPLETED",
                "fileCount": 5,
                "labelState": "labeled",
                "source": "uploaded",
                "contentSignature": "5:5:2026-01-01T00:00:00+00:00|src=uploaded",
            }
            result = test_set_index._reconcile_test_set_tracking_entry(
                s3, "bucket", "ts1", existing
            )
        assert result is None

    def test_reconcile_skips_row_when_source_marker_check_fails(self):
        """A transient HeadObject on '.source' bails the entire reconcile.

        The signature folds in the marker's presence; if we can't determine
        it, writing ``|src=None`` would poison the stored signature and
        trigger a full-scan retry every subsequent poll until a healthy
        HeadObject happens to land. Bail without a write instead.
        """
        s3 = Mock()
        with (
            patch.object(
                test_set_index,
                "_validate_test_set_files",
                return_value={
                    "valid": True,
                    "input_count": 5,
                    "labeled": True,
                    "signature": "5:5:2026-01-01T00:00:00+00:00",
                },
            ),
            patch.object(test_set_index, "_get_test_set_source", return_value=None),
            patch("boto3.resource") as boto3_resource,
            patch.dict(os.environ, {"TRACKING_TABLE": "test-table"}),
        ):
            update_mock = boto3_resource.return_value.Table.return_value.update_item
            existing = {
                "PK": "testset#ts1",
                "SK": "metadata",
                "status": "COMPLETED",
                "fileCount": 5,
                "labelState": "labeled",
                "source": "synthetic",
                "contentSignature": "stale",
            }
            result = test_set_index._reconcile_test_set_tracking_entry(
                s3, "bucket", "ts1", existing
            )
        assert result is None
        # No DDB write on the None-marker path.
        assert update_mock.call_count == 0

    def test_reconcile_skips_row_with_running_label_job(self):
        """Draft-labeling harvest is mid-write ⇒ don't touch the row.

        The harvester writes baselines one document per poll, so between
        polls the folder is legitimately partially paired even though the
        row's ``status`` remains ``COMPLETED``. Reconciling in that window
        would flag every in-progress job as FAILED.
        """
        s3 = Mock()
        with (
            patch.object(test_set_index, "_validate_test_set_files") as validate,
            patch.object(test_set_index, "_get_test_set_source") as source_mock,
        ):
            existing = {
                "PK": "testset#ts1",
                "SK": "metadata",
                "status": "COMPLETED",
                "labelJobStatus": "RUNNING",
            }
            result = test_set_index._reconcile_test_set_tracking_entry(
                s3, "bucket", "ts1", existing
            )
        assert result is None
        # Gate short-circuits BEFORE any S3 work — no listings, no HeadObjects.
        validate.assert_not_called()
        source_mock.assert_not_called()

    def test_reconcile_skips_row_within_ttl(self):
        """Warm-container memo avoids re-doing the S3 listing on every 3s UI poll.

        The memo says "we scanned this prefix within the TTL and DDB still
        shows the signature we cached." If both hold, skip the whole
        listing/HeadObject burst.
        """
        s3 = Mock()
        signature = "1:1:2026-01-01T00:00:00+00:00|src=uploaded"
        # Seed the warm-container memo as if a prior reconcile had just written.
        test_set_index._RECONCILE_MEMO["ts1"] = (signature, time.monotonic())
        try:
            with (
                patch.object(test_set_index, "_validate_test_set_files") as validate,
                patch.object(test_set_index, "_get_test_set_source") as source_mock,
            ):
                existing = {
                    "PK": "testset#ts1",
                    "SK": "metadata",
                    "status": "COMPLETED",
                    "contentSignature": signature,
                }
                result = test_set_index._reconcile_test_set_tracking_entry(
                    s3, "bucket", "ts1", existing
                )
            assert result is None
            # Same guardrail as the RUNNING gate — no S3 work behind the TTL.
            validate.assert_not_called()
            source_mock.assert_not_called()
        finally:
            test_set_index._RECONCILE_MEMO.pop("ts1", None)

    def test_reconcile_ttl_bypassed_when_ddb_signature_diverges(self):
        """Memo does NOT skip when another writer has changed the DDB row.

        Guards against a stale memo hiding a real update: if the row's
        contentSignature drifted from what we cached, our stability
        assumption is dead and we must re-check.
        """
        s3 = Mock()
        cached_sig = "1:1:2026-01-01T00:00:00+00:00|src=uploaded"
        row_sig = "2:2:2026-01-02T00:00:00+00:00|src=uploaded"  # someone else moved it
        test_set_index._RECONCILE_MEMO["ts1"] = (cached_sig, time.monotonic())
        try:
            with (
                patch.object(
                    test_set_index,
                    "_validate_test_set_files",
                    return_value={
                        "valid": True,
                        "input_count": 2,
                        "labeled": True,
                        "signature": "2:2:2026-01-02T00:00:00+00:00",
                    },
                ),
                patch.object(
                    test_set_index, "_get_test_set_source", return_value="uploaded"
                ),
            ):
                existing = {
                    "PK": "testset#ts1",
                    "SK": "metadata",
                    "status": "COMPLETED",
                    "fileCount": 2,
                    "labelState": "labeled",
                    "source": "uploaded",
                    "contentSignature": row_sig,
                }
                result = test_set_index._reconcile_test_set_tracking_entry(
                    s3, "bucket", "ts1", existing
                )
            # Signature matches our fresh computation (base + src=uploaded ==
            # row_sig), so the fast path returns None *and* refreshes the memo
            # to the current value.
            assert result is None
            assert test_set_index._RECONCILE_MEMO["ts1"][0] == row_sig
        finally:
            test_set_index._RECONCILE_MEMO.pop("ts1", None)

    def test_reconcile_preserves_stack_managed_source(self):
        """A row whose ``source`` names an external dataset (HuggingFace, ConfBench)
        must survive the reconcile intact.

        ``_get_test_set_source`` reports 'uploaded' when the ``.source`` marker
        is absent, but that marker is not the source-of-truth for HF-backed
        benchmark deployers — those write strings like
        ``huggingface:amazon-agi/fake-w2`` directly to the DDB row. Reconcile
        adopting the marker's answer would silently rebrand every such
        dataset as 'uploaded' on its first pass, blank the Source column in
        the UI, and destroy the provenance until the deployer re-runs on a
        stack update.
        """
        s3 = Mock()
        with (
            patch.object(
                test_set_index,
                "_validate_test_set_files",
                return_value={
                    "valid": True,
                    "input_count": 2000,
                    "labeled": True,
                    "signature": "2000:2000:2026-01-01T00:00:00+00:00",
                },
            ),
            patch.object(
                test_set_index, "_get_test_set_source", return_value="uploaded"
            ),
            patch("boto3.resource") as boto3_resource,
            patch.dict(os.environ, {"TRACKING_TABLE": "test-table"}),
        ):
            # Reset the tracking-table singleton so it picks up our patch.
            test_set_index._tracking_table = None
            update_mock = boto3_resource.return_value.Table.return_value.update_item
            existing = {
                "PK": "testset#fake-w2",
                "SK": "metadata",
                "status": "COMPLETED",
                "fileCount": 2000,
                "labelState": None,
                "source": "huggingface:amazon-agi/fake-w2",
                "contentSignature": "stale",
            }
            result = test_set_index._reconcile_test_set_tracking_entry(
                s3, "bucket", "fake-w2", existing
            )
        # A write happened (signature moved) but ``source`` is NOT among the
        # SET fields.
        assert update_mock.call_count == 1
        set_fields = _extract_set_fields(update_mock.call_args)
        assert "source" not in set_fields, (
            f"reconcile clobbered stack-managed source: SET fields {set_fields!r}"
        )
        # The merged in-memory result also preserves the original source.
        assert result is not None
        assert result["source"] == "huggingface:amazon-agi/fake-w2"

    def test_reconcile_softens_partial_pairing_on_stack_managed_row(self):
        """A stack-managed dataset (source not in {null, uploaded, synthetic})
        with partial baseline coverage is NOT flagged as FAILED.

        HuggingFace deployers and the ConfBench ingest can leave some
        documents without baselines when per-document processing fails; the
        finalizer still records the set as COMPLETED. Reconcile must not
        override that with a red FAILED banner on the row.
        """
        s3 = Mock()
        with (
            patch.object(
                test_set_index,
                "_validate_test_set_files",
                return_value={
                    "valid": False,
                    "input_count": 100,
                    "labeled": False,
                    "error": "Missing baseline files for: doc42.pdf, doc57.pdf",
                    "signature": "100:98:2026-01-01T00:00:00+00:00",
                },
            ),
            patch.object(
                test_set_index, "_get_test_set_source", return_value="uploaded"
            ),
            patch("boto3.resource") as boto3_resource,
            patch.dict(os.environ, {"TRACKING_TABLE": "test-table"}),
        ):
            test_set_index._tracking_table = None
            update_mock = boto3_resource.return_value.Table.return_value.update_item
            existing = {
                "PK": "testset#fcc",
                "SK": "metadata",
                "status": "COMPLETED",
                "fileCount": 100,
                "labelState": None,  # HF deployers don't set labelState
                "source": "huggingface:amazon-agi/RealKIE-FCC-Verified",
                "contentSignature": "stale",
            }
            result = test_set_index._reconcile_test_set_tracking_entry(
                s3, "bucket", "fcc", existing
            )
        assert result is not None
        # Status stays COMPLETED — partial pairing is expected on
        # stack-managed rows.
        assert result["status"] == "COMPLETED"
        # No error written, no status transition, no source clobber.
        set_fields = _extract_set_fields(update_mock.call_args)
        assert "error" not in set_fields, set_fields
        assert "status" not in set_fields, set_fields
        assert "source" not in set_fields, set_fields
        # Source preserved (regression check crossed with #1).
        assert result["source"] == "huggingface:amazon-agi/RealKIE-FCC-Verified"

    def test_reconcile_softens_extra_baseline_on_labeled_row(self):
        """Deleting an input from a labelled set leaves an orphan baseline.

        The validator reports that as ``Extra baseline files:...``. Same
        rationale as the ``Missing baseline files for:...`` case — a row
        inside the labeling lifecycle should not be flagged FAILED for it.
        """
        s3 = Mock()
        with (
            patch.object(
                test_set_index,
                "_validate_test_set_files",
                return_value={
                    "valid": False,
                    "input_count": 2,
                    "labeled": False,
                    "error": "Extra baseline files: c.pdf",
                    "signature": "2:3:2026-01-01T00:00:00+00:00",
                },
            ),
            patch.object(
                test_set_index, "_get_test_set_source", return_value="uploaded"
            ),
            patch("boto3.resource") as boto3_resource,
            patch.dict(os.environ, {"TRACKING_TABLE": "test-table"}),
        ):
            test_set_index._tracking_table = None
            update_mock = boto3_resource.return_value.Table.return_value.update_item
            existing = {
                "PK": "testset#ts1",
                "SK": "metadata",
                "status": "COMPLETED",
                "fileCount": 3,
                "labelState": "labeled",
                "source": "uploaded",
                "contentSignature": "stale",
            }
            result = test_set_index._reconcile_test_set_tracking_entry(
                s3, "bucket", "ts1", existing
            )
        assert result is not None
        assert result["status"] == "COMPLETED"
        assert result["labelState"] == "labeled"
        # The row's status/error must NOT appear in the SET portion of the
        # UpdateExpression. (The ``#st`` alias appears in
        # ExpressionAttributeNames for the ConditionExpression's
        # ``#st IN (:completed, :failed)`` — that's not a SET.)
        set_fields = _extract_set_fields(update_mock.call_args)
        assert "status" not in set_fields, set_fields
        assert "error" not in set_fields, set_fields

    def test_reconcile_preserves_draft_label_state(self, labeling_env):
        """A row with labelState='draft' must not be promoted to 'labeled'.

        The validator only sees folder shape (input↔baseline pairing), so a
        set of machine-generated draft labels looks identical to a fully
        reviewed one. Reconcile writing 'labeled' would silently promote
        unreviewed drafts into "verified" ground truth — exactly what the
        draft-labeling workflow exists to prevent.
        """
        table, s3 = labeling_env

        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=b"{}",
        )
        _seed_test_set(
            table,
            "ts1",
            name="ts1",
            status="COMPLETED",
            fileCount=1,
            labelState="draft",  # machine drafts awaiting review
            source="uploaded",
            createdAt="2026-01-01T00:00:00Z",
            InitialEventTime="2026-01-01T00:00:00Z",
            ItemType="testset",
            contentSignature="stale",
        )

        existing_row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})[
            "Item"
        ]
        result = test_set_index._reconcile_test_set_tracking_entry(
            s3, "test-set-bucket", "ts1", existing_row
        )
        # Reconcile may have refreshed contentSignature, but MUST NOT touch
        # labelState — the row still says 'draft', and the merged in-memory
        # view returned to the resolver still says 'draft'.
        row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert row["labelState"] == "draft"
        if result is not None:
            assert result["labelState"] == "draft"

    def test_reconcile_bails_on_condition_check_failure(self):
        """The ConditionExpression closes the race with a concurrent copier write.

        If TestSetFileCopier flips the row to UPDATING between our read and
        our conditional write, DynamoDB returns
        ConditionalCheckFailedException and we must return None (don't retry,
        don't log an error — the copier will finish and its terminal write
        arms the next poll's reconcile).
        """
        from botocore.exceptions import ClientError

        s3 = Mock()
        with (
            patch.object(
                test_set_index,
                "_validate_test_set_files",
                return_value={
                    "valid": True,
                    "input_count": 2,
                    "labeled": True,
                    "signature": "2:2:2026-02-01T00:00:00+00:00",
                },
            ),
            patch.object(
                test_set_index, "_get_test_set_source", return_value="uploaded"
            ),
            patch("boto3.resource") as boto3_resource,
            patch.dict(os.environ, {"TRACKING_TABLE": "test-table"}),
        ):
            update_mock = boto3_resource.return_value.Table.return_value.update_item
            update_mock.side_effect = ClientError(
                {
                    "Error": {
                        "Code": "ConditionalCheckFailedException",
                        "Message": "row not in COMPLETED/FAILED",
                    }
                },
                "UpdateItem",
            )
            existing = {
                "PK": "testset#ts1",
                "SK": "metadata",
                "status": "COMPLETED",
                "fileCount": 1,
                "labelState": "labeled",
                "source": "uploaded",
                "contentSignature": "stale",
            }
            result = test_set_index._reconcile_test_set_tracking_entry(
                s3, "bucket", "ts1", existing
            )
        assert result is None
        # We DID attempt the write with the condition — the guard is the DDB
        # condition, not the in-process pre-check. Condition has two clauses:
        # (a) status must be COMPLETED/FAILED (catches copier/extractor race)
        # (b) contentSignature must match (catches concurrent-reconcile race)
        condition = update_mock.call_args.kwargs["ConditionExpression"]
        assert "#st IN (:completed, :failed)" in condition
        assert "attribute_not_exists(#sig) OR #sig = :old_sig" in condition

    def test_reconcile_signature_folds_in_source_marker(self):
        """Signature must invalidate when '.source' appears after registration.

        The synthetic generator drops '<prefix>/.source' asynchronously after
        the row is already registered as source='uploaded'. Because a single
        HeadObject drives both the signature's ``src=`` field and the
        computed ``source``, a marker landing between polls flips both
        atomically: signature moves ⇒ full reconcile runs ⇒ source rewritten
        to 'synthetic'.
        """
        s3 = Mock()
        # Reconcile calls _get_test_set_source once per pass. First pass: no
        # marker (uploaded). Second pass: marker written (synthetic).
        source_calls = {"n": 0}

        def _source_side_effect(*args, **kwargs):
            source_calls["n"] += 1
            return "synthetic" if source_calls["n"] > 1 else "uploaded"

        with (
            patch.object(
                test_set_index,
                "_validate_test_set_files",
                return_value={
                    "valid": True,
                    "input_count": 1,
                    "labeled": True,
                    "signature": "1:1:2026-01-01T00:00:00+00:00",
                },
            ),
            patch.object(
                test_set_index,
                "_get_test_set_source",
                side_effect=_source_side_effect,
            ),
            patch("boto3.resource") as boto3_resource,
            patch.dict(os.environ, {"TRACKING_TABLE": "test-table"}),
        ):
            update_mock = boto3_resource.return_value.Table.return_value.update_item
            existing = {
                "PK": "testset#ts1",
                "SK": "metadata",
                "status": "COMPLETED",
                "fileCount": 1,
                "labelState": "labeled",
                "source": "uploaded",
                # Signature currently reflects "no marker".
                "contentSignature": "1:1:2026-01-01T00:00:00+00:00|src=uploaded",
            }
            # First reconcile: signature still matches (no marker) ⇒ no-op.
            r1 = test_set_index._reconcile_test_set_tracking_entry(
                s3, "bucket", "ts1", existing
            )
            assert r1 is None
            assert update_mock.call_count == 0

            # Clear the warm-container memo to simulate the TTL window
            # expiring — otherwise the second call short-circuits behind
            # the memo and never re-checks the marker.
            test_set_index._RECONCILE_MEMO.clear()

            # Second reconcile after generator wrote '.source': the source
            # helper reports synthetic, signature moves, source is written.
            r2 = test_set_index._reconcile_test_set_tracking_entry(
                s3, "bucket", "ts1", existing
            )
            assert r2 is not None
            assert r2["source"] == "synthetic"
            assert update_mock.call_count == 1

    def test_create_path_signature_matches_reconcile_format(self):
        """New-folder discovery must store the same signature format reconcile
        compares against, otherwise the very next poll wastes a full validate
        + DDB write to normalise it.
        """
        base = "3:3:2026-01-01T00:00:00+00:00"
        # Both callers must produce the same string given the same inputs.
        assert (
            test_set_index._compute_content_signature(base, "uploaded")
            == f"{base}|src=uploaded"
        )
        assert (
            test_set_index._compute_content_signature(base, "synthetic")
            == f"{base}|src=synthetic"
        )

    def test_get_test_sets_does_not_flip_labelled_set_to_failed_after_demote(
        self, labeling_env
    ):
        """Round-4 blocking regression, at the full ``get_test_sets`` level.

        The reviewer's headline flow: a labelled set at (2 inputs, 2 baselines)
        picks up a third direct-S3 input. Under the shipped code:

        * poll 1 sees COMPLETED/labeled/2, softens the partial pairing,
          leaves labelState=labeled and status=COMPLETED — good.
        * poll 2 (TTL window past) runs `_reconcile_label_state` first,
          which sees partial pairing and DEMOTES labelState in place to
          'unlabeled' on the row it hands to the reconcile.
        * `_reconcile_test_set_tracking_entry` then keys its softening on
          labelState=='labeled'/'draft', doesn't see either, and hard-fails
          the row: status=FAILED, error="Missing baseline files for: c.pdf".

        The UI renders a red FAILED badge on a set that is simply mid-
        labelling. This exercises the whole ``get_test_sets`` call chain
        — helper-level tests couldn't see the interaction, which is why
        it slipped through three earlier review rounds.
        """
        table, s3 = labeling_env

        # Two fully-paired documents + a third direct-S3 input.
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
        for name in ("a.pdf", "b.pdf"):
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/{name}/sections/1/result.json",
                Body=b"{}",
            )
        _seed_test_set(
            table,
            "ts1",
            name="ts1",
            status="COMPLETED",
            fileCount=2,
            labelState="labeled",  # what _reconcile_label_state will demote
            source="uploaded",
            createdAt="2026-01-01T00:00:00Z",
            InitialEventTime="2026-01-01T00:00:00Z",
            ItemType="testset",
            contentSignature="stale",  # forces the reconcile to run
        )

        # Full get_test_sets pass — this is the interaction the reviewer's
        # probe exercised.
        result = test_set_index.get_test_sets()

        # The set should still be COMPLETED with no FAILED error string;
        # `_reconcile_label_state` may have moved labelState (its call is
        # independent of this MR's soften-vs-fail decision), but the
        # reconcile MUST NOT flip status to FAILED for the partial pairing.
        row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert row["status"] == "COMPLETED", (
            f"reconcile hard-failed a set that is mid-labelling: "
            f"status={row['status']!r}, error={row.get('error')!r}"
        )
        assert "error" not in row, (
            f"reconcile wrote a spurious error on a labelled set: {row.get('error')!r}"
        )
        assert row["fileCount"] == 3

        # And the response the UI receives says the same thing.
        entry = next((r for r in result if r["id"] == "ts1"), None)
        assert entry is not None
        assert entry["status"] == "COMPLETED"
        assert entry.get("error") is None
        assert entry["fileCount"] == 3

    def test_memo_hits_do_not_consume_reconcile_probe_budget(self, labeling_env):
        """On a stack with more than MAX_RECONCILE_PROBES test sets, memo hits
        must not lock out sets past the alphabetically-first ``MAX_RECONCILE_PROBES``
        on warm containers.

        Discovered adversarially: the probe budget increments per call to the
        reconcile helper, but memo hits are O(1) dict lookups with no S3
        traffic. If they count against the budget, the first
        MAX_RECONCILE_PROBES sets consume all slots on every warm poll (as
        memo hits, doing zero S3 work), and the tail sets never get their own
        chance to reconcile until cold start.

        Fixed by checking the memo BEFORE consuming a budget slot in the
        caller. Only S3-touching reconciles cost budget.
        """
        table, s3 = labeling_env

        # Create MAX_RECONCILE_PROBES + 5 stale sets, alphabetically named.
        set_count = test_set_index.MAX_RECONCILE_PROBES + 5
        for i in range(set_count):
            tsid = f"ts{i:03d}"
            s3.put_object(
                Bucket="test-set-bucket", Key=f"{tsid}/input/a.pdf", Body=b"x"
            )
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"{tsid}/baseline/a.pdf/sections/1/result.json",
                Body=b"{}",
            )
            _seed_test_set(
                table,
                tsid,
                name=tsid,
                status="COMPLETED",
                fileCount=1,
                labelState="labeled",
                source="uploaded",
                createdAt="2026-01-01T00:00:00Z",
                InitialEventTime="2026-01-01T00:00:00Z",
                ItemType="testset",
                contentSignature="stale",
            )

        # First pass: reconcile does S3 work for the first MAX_RECONCILE_PROBES
        # sets, memoizes them. The tail is deferred.
        test_set_index.get_test_sets()
        first_pass_memo = set(test_set_index._RECONCILE_MEMO.keys())
        assert len(first_pass_memo) == test_set_index.MAX_RECONCILE_PROBES, (
            f"expected {test_set_index.MAX_RECONCILE_PROBES} memoized after "
            f"first pass, got {len(first_pass_memo)}"
        )

        # Second pass: memo hits for the first MAX_RECONCILE_PROBES MUST NOT
        # consume the budget — the tail sets should get their turn.
        test_set_index.get_test_sets()
        after_second = set(test_set_index._RECONCILE_MEMO.keys())
        expected = {f"ts{i:03d}" for i in range(set_count)}
        assert after_second == expected, (
            f"warm container starved sets past MAX_RECONCILE_PROBES: "
            f"missing {sorted(expected - after_second)}"
        )

    # -- Signature/memo invalidation on delete/reset paths ----------------
    #
    # Adversarial: `remove_documents_from_test_set` was the only path
    # invalidating `contentSignature` on the row and popping the warm-
    # container memo. `clear_draft_labels`, `reset_test_set_labels`, and
    # `delete_test_sets` all mutate S3 baselines or delete the row entirely
    # but were leaving the memo stale. Within the 30 s TTL window that
    # could let the next reconcile skip a folder whose S3 state had just
    # changed.

    def test_clear_draft_labels_invalidates_signature_and_memo(self, labeling_env):
        """After clear_draft_labels the next reconcile must re-scan.

        Seeds a memoized signature that matches the DDB row, verifies
        clear_draft_labels invalidates both, and confirms `_within_reconcile_ttl`
        returns False after the operation — proving reconcile would re-scan
        instead of skipping.
        """
        table, s3 = labeling_env

        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=json.dumps({"labelSource": "draft-machine"}).encode(),
        )
        _seed_test_set(
            table,
            "ts1",
            fileCount=1,
            labelState="draft",
            contentSignature="prior-sig",
            labelJobId="job1",
            labelJobStatus="COMPLETED",
        )
        # Simulate a warm container that just reconciled this prefix.
        test_set_index._RECONCILE_MEMO["ts1"] = ("prior-sig", time.monotonic())
        assert test_set_index._within_reconcile_ttl("ts1", "prior-sig") is True

        test_set_index.clear_draft_labels({"testSetId": "ts1"})

        # DDB row: contentSignature must be REMOVEd.
        row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert "contentSignature" not in row, (
            "clear_draft_labels left stale contentSignature on the row"
        )
        # Memo entry must be popped.
        assert "ts1" not in test_set_index._RECONCILE_MEMO, (
            "clear_draft_labels left a stale memo entry that could skip "
            "the next reconcile within the TTL window"
        )
        # Reconcile would NOT skip on the next call.
        assert (
            test_set_index._within_reconcile_ttl("ts1", row.get("contentSignature"))
            is False
        )

    def test_reset_test_set_labels_invalidates_signature_and_memo(self, labeling_env):
        """After reset_test_set_labels the next reconcile must re-scan.

        Every baseline just went away; the memoized signature no longer
        describes S3. Same invariant as clear_draft_labels.
        """
        table, s3 = labeling_env

        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=b"{}",
        )
        _seed_test_set(
            table,
            "ts1",
            fileCount=1,
            labelState="labeled",
            contentSignature="prior-sig",
        )
        test_set_index._RECONCILE_MEMO["ts1"] = ("prior-sig", time.monotonic())

        # Admin identity — reset is admin-only.
        with patch.object(
            test_set_index,
            "handler",
            wraps=test_set_index.handler,
        ):
            test_set_index.reset_test_set_labels({"testSetId": "ts1"})

        row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert "contentSignature" not in row, (
            "reset_test_set_labels left stale contentSignature"
        )
        assert "ts1" not in test_set_index._RECONCILE_MEMO, (
            "reset_test_set_labels left a stale memo entry"
        )
        assert row["labelState"] == "unlabeled"

    def test_delete_test_sets_pops_warm_container_memo(self, labeling_env):
        """After delete_test_sets the id must not carry a stale memo entry
        that could shadow a namesake reused later.
        """
        table, s3 = labeling_env

        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        _seed_test_set(table, "ts1", fileCount=1)
        test_set_index._RECONCILE_MEMO["ts1"] = ("prior-sig", time.monotonic())

        test_set_index.delete_test_sets({"testSetIds": ["ts1"]})

        # Row is gone.
        assert (
            table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"}).get("Item")
            is None
        )
        # Memo entry popped — the container doesn't leak entries for deleted ids.
        assert "ts1" not in test_set_index._RECONCILE_MEMO, (
            "delete_test_sets left a stale memo entry that could shadow "
            "a same-id set created later"
        )

    def test_no_inputs_preserves_draft_labelstate(self, labeling_env):
        """Deleting all inputs from a draft-labelled set must not destroy the
        'draft' signal.

        Regression pin for a silent draft→labeled promotion: without this
        guard, sequence (a) delete all inputs → no_inputs branch overwrites
        labelState=unlabeled; (b) restore inputs → else-branch sees
        labelState='unlabeled' (not 'draft') and promotes to 'labeled'.
        Unreviewed machine drafts become "verified" ground truth silently.
        _reconcile_label_state can't undo it because it skips rows with a
        labelJobId set.
        """
        table, s3 = labeling_env

        # Fully-paired draft set: 2 inputs + 2 draft-machine baselines.
        for name in ("a.pdf", "b.pdf"):
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/{name}/sections/1/result.json",
                Body=json.dumps({"labelSource": "draft-machine"}).encode(),
            )
        _seed_test_set(
            table,
            "ts1",
            status="COMPLETED",
            fileCount=2,
            labelState="draft",
            labelJobId="job1",
            labelJobStatus="COMPLETED",
            source="uploaded",
            contentSignature="stale",
        )

        # Step 1: delete all inputs via S3.
        s3.delete_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf")
        s3.delete_object(Bucket="test-set-bucket", Key="ts1/input/b.pdf")

        existing_row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})[
            "Item"
        ]
        test_set_index._reconcile_test_set_tracking_entry(
            s3, "test-set-bucket", "ts1", existing_row
        )
        row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert row["status"] == "FAILED"
        assert row["labelState"] == "draft", (
            "no_inputs branch destroyed the draft signal — a subsequent "
            "recovery would silently bless machine drafts as ground truth"
        )

        # Step 2: user restores the inputs. Recovery should preserve 'draft'.
        for name in ("a.pdf", "b.pdf"):
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
        # Force a fresh reconcile by clearing the memo.
        test_set_index._RECONCILE_MEMO.clear()
        existing_row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})[
            "Item"
        ]
        test_set_index._reconcile_test_set_tracking_entry(
            s3, "test-set-bucket", "ts1", existing_row
        )
        row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert row["status"] == "COMPLETED"
        assert row["labelState"] == "draft", (
            "recovery from no-inputs promoted 'draft' to 'labeled' silently"
        )

    def test_concurrent_writes_reject_stale_signature(self, labeling_env):
        """ConditionExpression must protect against a stale reconcile
        clobbering a fresher one.

        Simulates two concurrent Lambda containers: A reads row with sig X.
        Before A writes, B has already written a fresh sig Y (concurrent
        reconcile after new files landed). A's write with old_sig=X must be
        rejected by the DDB ConditionExpression.
        """
        table, s3 = labeling_env

        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=b"{}",
        )
        _seed_test_set(
            table,
            "ts1",
            fileCount=1,
            labelState="labeled",
            source="uploaded",
            contentSignature="fresher-sig-written-by-B",
        )

        # Container A's read happened before B wrote. A's existing_row has the
        # OLD signature; DDB now has "fresher-sig-written-by-B".
        existing_row_stale = dict(
            table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        )
        existing_row_stale["contentSignature"] = "old-sig-A-read"

        result = test_set_index._reconcile_test_set_tracking_entry(
            s3, "test-set-bucket", "ts1", existing_row_stale
        )
        # A's write should be rejected by the ConditionExpression
        # (attribute_not_exists OR sig == old-sig-A-read; neither holds
        # because DDB has fresher-sig-written-by-B). Reconcile returns None.
        assert result is None
        # DDB still has B's fresh signature untouched.
        row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert row["contentSignature"] == "fresher-sig-written-by-B", (
            "concurrent write from A clobbered B's fresher signature"
        )

    def test_partial_pairing_refreshes_stale_error_string(self, labeling_env):
        """A row that started FAILED at discovery with a stale error string
        must have that error refreshed to the current validator output as
        the pairing gets partially fixed.

        Concrete regression: new folder discovery hard-fails with "Missing
        baseline files for: a.pdf, b.pdf, c.pdf". User adds baselines for
        a.pdf and b.pdf, still missing c.pdf. Previously, my softening
        branch kept the old error string that named all three; now it
        refreshes to reflect the current partial-pairing state.
        """
        table, s3 = labeling_env

        # Simulate the after-partial-fix state: 3 inputs, 2 baselines.
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            s3.put_object(Bucket="test-set-bucket", Key=f"ts1/input/{name}", Body=b"x")
        for name in ("a.pdf", "b.pdf"):
            s3.put_object(
                Bucket="test-set-bucket",
                Key=f"ts1/baseline/{name}/sections/1/result.json",
                Body=b"{}",
            )
        # Row was FAILED at discovery with all three missing; stale error.
        _seed_test_set(
            table,
            "ts1",
            fileCount=3,
            status="FAILED",
            labelState="unlabeled",
            source="uploaded",
            error="Missing baseline files for: a.pdf, b.pdf, c.pdf",
            contentSignature="stale",
        )

        existing_row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})[
            "Item"
        ]
        test_set_index._reconcile_test_set_tracking_entry(
            s3, "test-set-bucket", "ts1", existing_row
        )
        row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        # Status stays FAILED (softening on partial pairing preserves it).
        assert row["status"] == "FAILED"
        # Error must reflect CURRENT state — only c.pdf is missing now.
        assert "c.pdf" in row["error"]
        assert "a.pdf" not in row["error"], (
            f"error still names a.pdf which has a baseline now: {row['error']!r}"
        )
        assert "b.pdf" not in row["error"], (
            f"error still names b.pdf which has a baseline now: {row['error']!r}"
        )

    # -- validation_failed sentinel — extended to all validator call sites --
    #
    # `_validate_test_set_files` returns a sentinel dict (validation_failed=
    # True, input_count=0, valid=False, labeled=False) on transient S3
    # errors. My reconcile checks it and bails. Three sibling call sites
    # (create-path, remove_documents_from_test_set, _reconcile_label_state)
    # were previously ignoring the sentinel, using the placeholder values
    # as real observations and writing poisoned state.

    def test_remove_documents_bails_on_transient_validator_failure(self):
        """Transient S3 error in the validator after a remove must not
        overwrite the row's fileCount with 0.
        """
        s3_mock = Mock()
        s3_mock.get_paginator.return_value.paginate.return_value = [{"Contents": []}]
        # Pretend the meta row has fileCount=5.
        with (
            patch.object(
                test_set_index.db_client,
                "get_item",
                return_value={
                    "PK": "testset#ts1",
                    "SK": "metadata",
                    "id": "ts1",
                    "name": "ts1",
                    "fileCount": 5,
                    "status": "COMPLETED",
                },
            ),
            patch.object(test_set_index, "s3_client", s3_mock),
            patch.object(
                test_set_index,
                "_validate_test_set_files",
                return_value={
                    "valid": False,
                    "validation_failed": True,
                    "error": "Validation error: Throttled",
                    "input_count": 0,
                    "labeled": False,
                    "signature": "",
                },
            ),
            patch("boto3.resource") as boto3_resource,
            patch.dict(os.environ, {"TEST_SET_BUCKET": "test-set-bucket"}),
        ):
            update_mock = boto3_resource.return_value.Table.return_value.update_item
            result = test_set_index.remove_documents_from_test_set(
                {"testSetId": "ts1", "fileNames": []}
            )
        # No fileCount overwrite — update_item on the DDB row didn't happen.
        assert update_mock.call_count == 0
        # Response reports the row's LAST-KNOWN fileCount, not 0.
        assert result["fileCount"] == 5

    def test_first_discovery_skips_prefix_on_transient_validator_failure(
        self, labeling_env
    ):
        """A transient S3 error during first-discovery must not register a
        FAILED row with poisoned state (input_count=0). The prefix is
        skipped on this pass; the next getTestSets retries.
        """
        table, s3 = labeling_env

        # A folder in S3 but no DDB row — first-discovery path.
        s3.put_object(Bucket="test-set-bucket", Key="ts-new/input/a.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts-new/baseline/a.pdf/sections/1/result.json",
            Body=b"{}",
        )

        with patch.object(
            test_set_index,
            "_validate_test_set_files",
            return_value={
                "valid": False,
                "validation_failed": True,
                "error": "Validation error: Throttled",
                "input_count": 0,
                "labeled": False,
                "signature": "",
            },
        ):
            test_set_index.get_test_sets()

        # No row registered on this pass.
        row = table.get_item(Key={"PK": "testset#ts-new", "SK": "metadata"}).get("Item")
        assert row is None, (
            "first-discovery registered a poisoned FAILED row on a "
            "transient validator failure"
        )

    def test_reconcile_runs_on_legacy_row_without_status(self, labeling_env):
        """Legacy rows written before ``status`` was persisted must still be
        reconcilable. Previously Gate 1's allowlist rejected any row where
        ``status`` was missing → the row was permanently invisible.
        """
        table, s3 = labeling_env

        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"x")
        s3.put_object(
            Bucket="test-set-bucket",
            Key="ts1/baseline/a.pdf/sections/1/result.json",
            Body=b"{}",
        )
        # Deliberately no `status` — simulate a legacy row.
        _seed_test_set(
            table,
            "ts1",
            fileCount=1,
            labelState="labeled",
            source="uploaded",
            contentSignature="stale",
        )

        existing_row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})[
            "Item"
        ]
        assert "status" not in existing_row  # precondition
        result = test_set_index._reconcile_test_set_tracking_entry(
            s3, "test-set-bucket", "ts1", existing_row
        )
        assert result is not None, (
            "legacy row without status was permanently invisible to reconcile"
        )
        # Row now has status COMPLETED.
        row = table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"]
        assert row["status"] == "COMPLETED"

    def test_reconcile_hard_fails_unknown_validator_error(self):
        """A validator error class that isn't Missing/Extra baseline or "No
        input files found" must NOT be silently marked COMPLETED. This is
        the fallback branch that catches future validator error classes.
        """
        s3 = Mock()
        with (
            patch.object(
                test_set_index,
                "_validate_test_set_files",
                return_value={
                    "valid": False,
                    # Deliberately NOT partial_pairing and NOT no_inputs.
                    "error": "Some future error class we don't handle yet",
                    "input_count": 5,
                    "labeled": False,
                    "signature": "5:5:2026-01-01T00:00:00+00:00",
                },
            ),
            patch.object(
                test_set_index, "_get_test_set_source", return_value="uploaded"
            ),
            patch("boto3.resource"),
            patch.dict(os.environ, {"TRACKING_TABLE": "test-table"}),
        ):
            existing = {
                "PK": "testset#ts1",
                "SK": "metadata",
                "status": "COMPLETED",
                "fileCount": 5,
                "labelState": "labeled",
                "source": "uploaded",
                "contentSignature": "stale",
            }
            result = test_set_index._reconcile_test_set_tracking_entry(
                s3, "bucket", "ts1", existing
            )
        assert result is not None
        # Must be FAILED, not silently COMPLETED.
        assert result["status"] == "FAILED", (
            "unknown validator error class silently marked as COMPLETED"
        )
        assert "future error class" in result["error"]

    def test_get_test_set_source_logs_access_denied_at_error_level(self, caplog):
        """AccessDenied on `.source` is persistent — must be logged at ERROR
        so operators see the IAM misconfiguration, not silently at WARNING
        alongside transient throttling.
        """
        import logging as _logging

        from botocore.exceptions import ClientError

        s3 = Mock()
        s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "..."}}, "HeadObject"
        )
        with caplog.at_level(_logging.ERROR):
            result = test_set_index._get_test_set_source(s3, "bucket", "ts1")
        assert result is None  # unchanged safety behavior
        assert any(
            "denied" in rec.message.lower() and rec.levelno == _logging.ERROR
            for rec in caplog.records
        ), "AccessDenied on .source was not logged at ERROR"

    def test_extractor_guards_against_unbound_test_set_id(self):
        """Extractor's except clause must NOT NameError on a malformed record
        where test_set_id was never bound, AND must not carry a previous
        record's test_set_id into a later record's failure.
        """
        source = open(
            os.path.join(
                os.path.dirname(__file__),
                "../../../../src/lambda/test_set_zip_extractor/index.py",
            ),
            encoding="utf-8",
        ).read()
        # The fix is to bind test_set_id = None at the top of each record's
        # try, then guard the except's status write with `if test_set_id is
        # not None`. Both must be present.
        assert "test_set_id = None" in source, (
            "extractor doesn't reset test_set_id per record — a bad record "
            "following a healthy one can carry the previous test_set_id "
            "into the FAILED status write"
        )
        assert "if test_set_id is not None" in source, (
            "extractor's except clause doesn't guard the status write — "
            "a pre-parse failure would NameError"
        )


@pytest.mark.unit
class TestLabelStateReconciliation:
    """labelState must not understate a set holding ground truth — nor overstate one.

    It is derived once at registration and afterwards moved only by harvest and reset,
    so the synthetic generator writing documents and baselines straight to S3 leaves a
    fully-labelled set reporting "Unlabeled". Repairing that is the point. Promoting a
    set that is NOT ground truth is worse: a green badge on unreviewed machine drafts,
    the effort estimator running on them, and the "Labeling failed" warning suppressed.
    """

    @pytest.fixture
    def env(self):
        with mock_aws():
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket="test-set-bucket")
            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            table = ddb.create_table(
                TableName="test-table",
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
            writes = []

            def record(key, update_expression, **kwargs):
                writes.append((key["PK"], update_expression, kwargs))

            with patch.dict(
                os.environ,
                {
                    "TEST_SET_BUCKET": "test-set-bucket",
                    "TRACKING_TABLE": "test-table",
                    # Same region pin as above; the probe uses s3_client directly but
                    # the repair write goes through the ambient-region resource.
                    "AWS_DEFAULT_REGION": "us-east-1",
                    "AWS_REGION": "us-east-1",
                },
            ):
                with (
                    patch.object(test_set_index, "s3_client", s3),
                    patch.object(test_set_index.db_client, "update_item", record),
                ):
                    yield s3, table, writes

    @staticmethod
    def _seed_doc(s3, test_set_id, name, label_source=None):
        s3.put_object(
            Bucket="test-set-bucket", Key=f"{test_set_id}/input/{name}", Body=b"pdf"
        )
        body = {"inference_result": {"x": 1}}
        if label_source:
            body["labelSource"] = label_source
        s3.put_object(
            Bucket="test-set-bucket",
            Key=f"{test_set_id}/baseline/{name}/sections/1/result.json",
            Body=json.dumps(body).encode(),
        )

    def test_a_fully_labelled_set_is_repaired(self, env):
        s3, _table, writes = env
        self._seed_doc(s3, "ts1", "a.pdf")
        items = [{"id": "ts1", "labelState": "unlabeled", "fileCount": 1}]

        test_set_index._reconcile_label_state(items)

        assert items[0]["labelState"] == "labeled"
        assert any("labelState" in w[1] for w in writes), "the repair must persist"

    def test_a_set_mid_harvest_is_never_promoted(self, env):
        """The blocking case: drafts are on S3 before labelState moves to 'draft'.

        The harvest writes draft-machine labels under the same baseline/ prefix and
        only sets labelState when it reaches COMPLETED. Promoting here puts a
        "Labeled" badge on unreviewed machine output — and if the harvest then fails,
        permanently, while also suppressing the "Labeling failed" warning.
        """
        s3, _table, writes = env
        self._seed_doc(s3, "ts1", "a.pdf", label_source="draft-machine")
        items = [
            {
                "id": "ts1",
                "labelState": "unlabeled",
                "fileCount": 1,
                "labelJobId": "run-1",
                "labelJobStatus": "RUNNING",
            }
        ]

        test_set_index._reconcile_label_state(items)

        assert items[0]["labelState"] == "unlabeled"
        assert writes == []

    def test_a_failed_harvest_is_never_promoted(self, env):
        s3, _table, writes = env
        self._seed_doc(s3, "ts1", "a.pdf", label_source="draft-machine")
        items = [
            {
                "id": "ts1",
                "labelState": "unlabeled",
                "fileCount": 1,
                "labelJobStatus": "FAILED",
            }
        ]

        test_set_index._reconcile_label_state(items)

        assert items[0]["labelState"] == "unlabeled"

    def test_drafts_with_no_job_pointer_are_recorded_as_draft(self, env):
        """Belt and braces: machine drafts are never ground truth, job record or not."""
        s3, _table, writes = env
        self._seed_doc(s3, "ts1", "a.pdf", label_source="draft-machine")
        items = [{"id": "ts1", "labelState": "unlabeled", "fileCount": 1}]

        test_set_index._reconcile_label_state(items)

        assert items[0]["labelState"] == "draft"

    def test_partial_coverage_is_not_a_labelled_set(self, env):
        """One labelled document out of two is not ground truth for the set."""
        s3, _table, writes = env
        self._seed_doc(s3, "ts1", "a.pdf")
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/b.pdf", Body=b"pdf")
        items = [{"id": "ts1", "labelState": "unlabeled", "fileCount": 2}]

        test_set_index._reconcile_label_state(items)

        assert items[0]["labelState"] == "unlabeled"

    def test_a_set_with_no_baselines_is_left_alone(self, env):
        s3, _table, writes = env
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"pdf")
        items = [{"id": "ts1", "labelState": "unlabeled", "fileCount": 1}]

        test_set_index._reconcile_label_state(items)

        assert items[0]["labelState"] == "unlabeled"
        assert all("labelState" not in w[1] for w in writes)

    def test_an_unlabelled_set_is_not_reprobed_at_the_same_membership(self, env):
        """Without a marker the probe never converges on genuinely unlabelled sets."""
        s3, _table, writes = env
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/a.pdf", Body=b"pdf")
        items = [{"id": "ts1", "labelState": "unlabeled", "fileCount": 1}]

        test_set_index._reconcile_label_state(items)
        assert any("labelProbedFileCount" in w[1] for w in writes)

        calls = []
        with patch.object(
            test_set_index, "_validate_test_set_files", lambda *a, **k: calls.append(1)
        ):
            test_set_index._reconcile_label_state(
                [
                    {
                        "id": "ts1",
                        "labelState": "unlabeled",
                        "fileCount": 1,
                        "labelProbedFileCount": 1,
                    }
                ]
            )
        assert calls == [], "a set already probed at this membership must be skipped"

    def test_adding_documents_invalidates_the_marker(self, env):
        s3, _table, writes = env
        self._seed_doc(s3, "ts1", "a.pdf")
        items = [
            {
                "id": "ts1",
                "labelState": "unlabeled",
                "fileCount": 1,
                "labelProbedFileCount": 0,  # probed when the set was empty
            }
        ]

        test_set_index._reconcile_label_state(items)

        assert items[0]["labelState"] == "labeled"

    def test_an_overstated_set_is_demoted(self, env):
        """The same probe must correct in both directions.

        Adding documents without baselines to a labelled set makes "labeled" wrong, and
        a state that only ratchets upward would keep asserting complete ground truth. A
        set promoted in error by a laxer earlier version self-heals by the same path.
        """
        s3, _table, writes = env
        self._seed_doc(s3, "ts1", "a.pdf")
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/b.pdf", Body=b"pdf")
        items = [{"id": "ts1", "labelState": "labeled", "fileCount": 2}]

        test_set_index._reconcile_label_state(items)

        assert items[0]["labelState"] == "unlabeled"
        assert any("labelState" in w[1] for w in writes)

    def test_a_set_still_being_written_is_not_demoted(self, env):
        """Mid-copy incomplete coverage is temporary, so acting on it makes a flicker.

        The copier lands input/ keys before the matching baseline/ folders, so a probe
        during UPDATING sees coverage that is genuinely incomplete *right now* and would
        demote the set. It self-heals at COMPLETED, which is the problem: the user
        watching the list sees "Labeled" flip to "Unlabeled" and back for no reason they
        can act on.
        """
        s3, _table, writes = env
        self._seed_doc(s3, "ts1", "a.pdf")
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/b.pdf", Body=b"pdf")
        items = [
            {
                "id": "ts1",
                "labelState": "labeled",
                "fileCount": 2,
                "status": "UPDATING",
            }
        ]

        test_set_index._reconcile_label_state(items)

        assert items[0]["labelState"] == "labeled"
        assert all("labelState" not in w[1] for w in writes)
        # And no marker, so the set is genuinely re-probed once the copy settles rather
        # than being recorded as validated at this fileCount.
        assert "labelProbedFileCount" not in items[0]

    def test_a_completed_set_is_still_reconciled(self, env):
        """The in-flux guard must key on status, not disable reconciliation."""
        s3, _table, writes = env
        self._seed_doc(s3, "ts1", "a.pdf")
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/b.pdf", Body=b"pdf")
        items = [
            {
                "id": "ts1",
                "labelState": "labeled",
                "fileCount": 2,
                "status": "COMPLETED",
            }
        ]

        test_set_index._reconcile_label_state(items)

        assert items[0]["labelState"] == "unlabeled"
        assert any("labelState" in w[1] for w in writes)

    def test_a_correct_state_is_not_rewritten(self, env):
        """Probing is not licence to write: an agreeing state costs no update."""
        s3, _table, writes = env
        self._seed_doc(s3, "ts1", "a.pdf")
        items = [{"id": "ts1", "labelState": "labeled", "fileCount": 1}]

        test_set_index._reconcile_label_state(items)

        assert items[0]["labelState"] == "labeled"
        assert all("labelState" not in w[1] for w in writes)

    def test_sets_at_unchanged_membership_are_never_probed(self, env):
        """Steady state must cost nothing, which is what makes re-checking affordable."""
        s3, _table, writes = env
        calls = []
        with patch.object(
            test_set_index, "_validate_test_set_files", lambda *a, **k: calls.append(1)
        ):
            test_set_index._reconcile_label_state(
                [
                    {
                        "id": "a",
                        "labelState": "labeled",
                        "fileCount": 5,
                        "labelProbedFileCount": 5,
                    },
                    {
                        "id": "b",
                        "labelState": "unlabeled",
                        "fileCount": 2,
                        "labelProbedFileCount": 2,
                    },
                ]
            )
        assert calls == []

    def test_sets_owned_by_a_labeling_job_are_never_probed(self, env):
        s3, _table, writes = env
        calls = []
        with patch.object(
            test_set_index, "_validate_test_set_files", lambda *a, **k: calls.append(1)
        ):
            test_set_index._reconcile_label_state(
                [{"id": "a", "labelState": "draft", "labelJobId": "run-1"}]
            )
        assert calls == []

    def test_probing_is_bounded(self, env):
        """The list path is otherwise pure DynamoDB and is the hottest query."""
        s3, _table, writes = env
        calls = []
        many = [
            {"id": f"ts{i}", "labelState": "unlabeled", "fileCount": 1}
            for i in range(60)
        ]
        with patch.object(
            test_set_index,
            "_validate_test_set_files",
            lambda *a, **k: (calls.append(1), {"labeled": False})[1],
        ):
            test_set_index._reconcile_label_state(many)
        assert len(calls) == test_set_index.MAX_LABEL_STATE_PROBES

    def test_a_probe_failure_never_breaks_the_list(self, env):
        s3, _table, writes = env
        with patch.object(
            test_set_index,
            "_validate_test_set_files",
            side_effect=RuntimeError("s3 down"),
        ):
            test_set_index._reconcile_label_state(
                [{"id": "ts1", "labelState": "unlabeled", "fileCount": 1}]
            )  # must not raise


@pytest.mark.unit
class TestReapAbandonedTestSets:
    """A non-terminal status with no owner left must not show as a permanent spinner.

    GENERATING is written by the generator extension and cleared by its runtime. If the
    runtime dies — or the extension is uninstalled — nothing remaining can clear it, and
    Test Studio renders it as in-progress indefinitely, across reloads, because the
    state is a database record. Observed live, then made permanent by removing the
    extension.
    """

    @pytest.fixture
    def env(self):
        with mock_aws():
            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            table = ddb.create_table(
                TableName="test-table",
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
            # Pin the region: the resolver builds its own boto3 resource with no
            # explicit region and other tests mutate the ambient one, which makes the
            # moto table invisible to it.
            with patch.dict(
                os.environ,
                {
                    "TRACKING_TABLE": "test-table",
                    "AWS_DEFAULT_REGION": "us-east-1",
                    "AWS_REGION": "us-east-1",
                },
            ):
                yield table

    @staticmethod
    def _hours_ago(hours):
        return (
            (datetime.now(timezone.utc) - timedelta(hours=hours))
            .isoformat()
            .replace("+00:00", "Z")
        )

    def _seed(self, table, status, stamp_hours=None, created_hours=None):
        item = {"PK": "testset#ts1", "SK": "metadata", "id": "ts1", "status": status}
        if stamp_hours is not None:
            item["statusUpdatedAt"] = self._hours_ago(stamp_hours)
        if created_hours is not None:
            item["createdAt"] = self._hours_ago(created_hours)
        table.put_item(Item=item)
        return [dict(item)]

    def test_a_long_abandoned_generation_is_failed(self, env):
        table = env
        items = self._seed(table, "GENERATING", stamp_hours=20)

        test_set_index._reap_abandoned_test_sets(items)

        assert items[0]["status"] == "FAILED"
        assert "no progress" in items[0]["error"]
        assert (
            table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"][
                "status"
            ]
            == "FAILED"
        )

    def test_a_generation_still_within_its_window_is_left_running(self, env):
        """Generation legitimately runs for hours; declaring it dead is worse."""
        table = env
        items = self._seed(table, "GENERATING", stamp_hours=3)

        test_set_index._reap_abandoned_test_sets(items)

        assert items[0]["status"] == "GENERATING"

    def test_updating_has_a_much_shorter_window(self, env):
        """A file copy is not a multi-hour job."""
        table = env
        items = self._seed(table, "UPDATING", stamp_hours=4)

        test_set_index._reap_abandoned_test_sets(items)

        assert items[0]["status"] == "FAILED"

    def test_queued_falls_back_to_created_at(self, env):
        """For QUEUED the two timestamps mark the same moment."""
        table = env
        items = self._seed(table, "QUEUED", created_hours=6)

        test_set_index._reap_abandoned_test_sets(items)

        assert items[0]["status"] == "FAILED"

    def test_a_record_with_no_timestamp_is_left_alone(self, env):
        """Records predating the field cannot be aged; failing them kills live work."""
        table = env
        items = self._seed(table, "GENERATING")

        test_set_index._reap_abandoned_test_sets(items)

        assert items[0]["status"] == "GENERATING"

    def test_terminal_statuses_are_never_touched(self, env):
        table = env
        for status in ("COMPLETED", "FAILED"):
            items = self._seed(table, status, stamp_hours=500)
            test_set_index._reap_abandoned_test_sets(items)
            assert items[0]["status"] == status

    def test_the_response_is_not_marked_failed_when_the_write_is_refused(self, env):
        """The in-memory record must not contradict the row.

        Mutating before the conditional write reported FAILED to the caller in exactly
        the case the condition exists to catch, so the UI showed a failure for a run
        that had just completed.
        """
        table = env
        items = self._seed(table, "GENERATING", stamp_hours=20)
        table.update_item(
            Key={"PK": "testset#ts1", "SK": "metadata"},
            UpdateExpression="SET #s = :c",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":c": "COMPLETED"},
        )

        test_set_index._reap_abandoned_test_sets(items)

        assert items[0]["status"] == "GENERATING", (
            "the response claimed FAILED for a set the write refused to change"
        )
        assert "error" not in items[0]

    def test_a_status_that_moved_since_the_read_wins(self, env):
        """The write is conditional, so a job reporting in mid-flight is not clobbered."""
        table = env
        items = self._seed(table, "GENERATING", stamp_hours=20)
        # The owning job completed between the list read and the reap.
        table.update_item(
            Key={"PK": "testset#ts1", "SK": "metadata"},
            UpdateExpression="SET #s = :c",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":c": "COMPLETED"},
        )

        test_set_index._reap_abandoned_test_sets(items)

        assert (
            table.get_item(Key={"PK": "testset#ts1", "SK": "metadata"})["Item"][
                "status"
            ]
            == "COMPLETED"
        )


@pytest.mark.unit
class TestStatusUpdatedAtIsWritten:
    """The reap windows are only reachable if something stamps the status time.

    STALE_STATUS_HOURS declares a window for UPDATING, but for a while nothing wrote
    statusUpdatedAt outside the generator extension — so an abandoned file copy spun
    forever while the code and CHANGELOG both claimed otherwise.
    """

    def test_add_documents_stamps_the_status_time(self):
        source = open(
            os.path.join(
                os.path.dirname(__file__),
                "../../../../nested/api-resolvers/src/lambda/test_set_resolver/index.py",
            ),
            encoding="utf-8",
        ).read()
        # Both UPDATING writes in this resolver must stamp it, or the UPDATING window
        # in STALE_STATUS_HOURS is unreachable.
        updating_writes = source.count('":status": "UPDATING"')
        stamped = source.count("statusUpdatedAt = :now")
        assert updating_writes == stamped == 2, (
            f"{updating_writes} UPDATING writes but {stamped} stamped"
        )

    def test_the_copier_stamps_the_status_time(self):
        source = open(
            os.path.join(
                os.path.dirname(__file__),
                "../../../../src/lambda/test_set_file_copier/index.py",
            ),
            encoding="utf-8",
        ).read()
        assert "statusUpdatedAt = :now" in source

    def test_the_copier_invalidates_content_signature(self):
        """Copier's terminal writes must REMOVE contentSignature so the
        resolver's warm-container memo doesn't skip the next reconcile on
        a stale signature — a direct-S3 add landing right after the copier
        completes would otherwise be invisible for up to the memo TTL.
        """
        source = open(
            os.path.join(
                os.path.dirname(__file__),
                "../../../../src/lambda/test_set_file_copier/index.py",
            ),
            encoding="utf-8",
        ).read()
        assert "REMOVE lastAddResult, contentSignature" in source, (
            "copier no longer REMOVEs contentSignature — warm-container memo "
            "can shadow a direct-S3 add landing right after the copier completes"
        )

    def test_the_extractor_invalidates_content_signature(self):
        """Extractor's writes must REMOVE contentSignature on both the
        COMPLETED and FAILED paths. A failed extract may have left partial
        S3 state; the reconcile still needs to re-scan.
        """
        source = open(
            os.path.join(
                os.path.dirname(__file__),
                "../../../../src/lambda/test_set_zip_extractor/index.py",
            ),
            encoding="utf-8",
        ).read()
        assert "REMOVE contentSignature" in source
        # Guard against a future edit re-guarding it inside `if file_count`.
        # The REMOVE must fire regardless of file_count so FAILED paths (no
        # file_count) also invalidate the signature.
        # Find the REMOVE line and confirm it's not nested under the
        # file_count conditional.
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "REMOVE contentSignature" in line:
                # Two lines above should NOT be `if file_count`; the pattern
                # in the fixed code puts REMOVE at the same indentation as
                # the outer try body, not inside the `if file_count` block.
                indent = len(line) - len(line.lstrip())
                # Walk back to find the enclosing 'if' — must be the outer try,
                # not `if file_count is not None`.
                for j in range(i - 1, -1, -1):
                    stripped = lines[j].lstrip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    line_indent = len(lines[j]) - len(stripped)
                    if line_indent < indent and stripped.startswith("if "):
                        assert "file_count" not in stripped, (
                            "REMOVE contentSignature is nested under "
                            "`if file_count is not None` — the FAILED path "
                            "won't invalidate the signature"
                        )
                        break
                break
