# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Which stack updates rebuild the S3 Vectors index, through ``lambda_handler``.

S3 Vectors has no UpdateIndex operation, so the dimension, distance metric and
metadata configuration chosen at CreateIndex can only be changed by deleting and
recreating the index, and that deletes every vector in it. The handler used to
rebuild the index on every Update that kept the bucket and index names, so
changing the embedding model or the KMS key parameter emptied the knowledge base
while changing nothing about the index. These tests pin that only a change to
an index setting rebuilds it, and that an update which keeps the index still
restores one that was deleted outside the stack.

The client is a real ``s3vectors`` client under ``botocore.stub.Stubber``; the
operations each update issues are recorded on ``provide-client-params``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import botocore.session
import handler
import pytest
from botocore.stub import Stubber

REGION = "us-east-1"
BUCKET = "idp-documentkb-abc123-s3-vectors"
INDEX = "bedrock-kb-index"
MODEL = botocore.session.get_session().get_service_model("s3vectors")


@pytest.fixture
def s3vectors(monkeypatch):
    client = botocore.session.get_session().create_client(
        "s3vectors", region_name=REGION
    )
    calls: list[str] = []
    client.meta.events.register(
        "provide-client-params.s3vectors",
        lambda model, **_kwargs: calls.append(model.name),
    )
    stubber = Stubber(client)
    sts = botocore.session.get_session().create_client("sts", region_name=REGION)
    sts_stubber = Stubber(sts)
    clients = {"s3vectors": client, "sts": sts}
    monkeypatch.setattr(handler.boto3, "client", lambda name, **_kwargs: clients[name])
    send = MagicMock()
    monkeypatch.setattr(handler.cfnresponse, "send", send)
    with stubber, sts_stubber:
        yield SimpleNamespace(
            client=client, stubber=stubber, sts=sts_stubber, send=send, calls=calls
        )
        stubber.assert_no_pending_responses()
        sts_stubber.assert_no_pending_responses()


# The properties nested/bedrockkb/template.yaml passes, as CloudFormation sends
# them: numbers arrive as strings.
TEMPLATE_PROPERTIES = {
    "ResourceType": "S3VectorBucketAndIndex",
    "BucketName": BUCKET,
    "IndexName": INDEX,
    "EmbeddingModel": "amazon.titan-embed-text-v2:0",
    "Region": REGION,
    "VectorDimension": "1024",
    "DistanceMetric": "cosine",
    "MetadataConfiguration": {
        "nonFilterableMetadataKeys": ["AMAZON_BEDROCK_METADATA", "AMAZON_BEDROCK_TEXT"]
    },
}


def _update(s3vectors, old: dict, new: dict) -> str:
    handler.lambda_handler(
        {
            "RequestType": "Update",
            "ResourceProperties": new,
            "OldResourceProperties": old,
            "PhysicalResourceId": f"s3vectors://{BUCKET}/{INDEX}",
            "StackId": "arn:aws:cloudformation:us-east-1:123456789012:stack/s/x",
            "RequestId": "req-00000000",
            "LogicalResourceId": "S3VectorBucketAndIndex",
            "ResponseURL": "https://example.invalid/response",
        },
        SimpleNamespace(log_stream_name="ls"),
    )
    assert s3vectors.send.call_count == 1, s3vectors.send.call_args_list
    return s3vectors.send.call_args.args[2]


def _expect_existing(s3vectors):
    s3vectors.stubber.add_response(
        "get_vector_bucket",
        {
            "vectorBucket": {
                "vectorBucketName": BUCKET,
                "vectorBucketArn": f"arn:aws:s3vectors:{REGION}:123456789012:bucket/{BUCKET}",
                "creationTime": "2026-01-01T00:00:00Z",
            }
        },
        {"vectorBucketName": BUCKET},
    )
    s3vectors.sts.add_response(
        "get_caller_identity",
        {
            "Account": "123456789012",
            "Arn": "arn:aws:iam::123456789012:root",
            "UserId": "x",
        },
        {},
    )


INDEX_ARN = f"arn:aws:s3vectors:{REGION}:123456789012:bucket/{BUCKET}/index/{INDEX}"


def _expect_kept_index(s3vectors, **create_error):
    """The keep path: read the bucket, then CreateIndex only to restore a missing index."""
    _expect_existing(s3vectors)
    if create_error:
        s3vectors.stubber.add_client_error("create_index", **create_error)
    else:
        s3vectors.stubber.add_response("create_index", {"indexArn": INDEX_ARN})


EXISTS = {"service_error_code": "ConflictException", "http_status_code": 409}


def test_index_settings_are_the_create_index_parameters():
    """The settings compared are CreateIndex parameters, and there is no UpdateIndex."""
    members = set(MODEL.operation_model("CreateIndex").input_shape.members)
    assert {"dimension", "distanceMetric", "metadataConfiguration"} <= members
    operations = botocore.session.get_session().get_service_data("s3vectors")[
        "operations"
    ]
    assert "CreateIndex" in operations
    assert "UpdateIndex" not in operations


@pytest.mark.parametrize(
    "changed",
    [
        {"EmbeddingModel": "cohere.embed-english-v3"},
        {"KmsKeyArn": "example-customer-managed-key"},
        {"VectorDimension": 1024},
        {
            "MetadataConfiguration": {
                "nonFilterableMetadataKeys": [
                    "AMAZON_BEDROCK_TEXT",
                    "AMAZON_BEDROCK_METADATA",
                ]
            }
        },
    ],
    ids=["embedding-model", "kms-key", "dimension-as-number", "metadata-key-order"],
)
def test_an_update_that_keeps_the_index_settings_keeps_the_index(s3vectors, changed):
    _expect_kept_index(s3vectors, **EXISTS)

    status = _update(
        s3vectors, TEMPLATE_PROPERTIES, dict(TEMPLATE_PROPERTIES, **changed)
    )

    assert status == "SUCCESS"
    assert s3vectors.calls == ["GetVectorBucket", "CreateIndex"], "no DeleteIndex"
    assert s3vectors.send.call_args.args[3]["IndexName"] == INDEX


def test_defaults_and_their_explicit_values_are_the_same_settings(s3vectors):
    """Properties written before the template named the settings rebuild nothing."""
    _expect_kept_index(s3vectors, **EXISTS)
    old = {
        key: value
        for key, value in TEMPLATE_PROPERTIES.items()
        if key not in ("VectorDimension", "DistanceMetric", "MetadataConfiguration")
    }

    assert _update(s3vectors, old, TEMPLATE_PROPERTIES) == "SUCCESS"
    assert s3vectors.calls == ["GetVectorBucket", "CreateIndex"], "no DeleteIndex"


@pytest.mark.parametrize(
    ("changed", "create_param", "expected"),
    [
        ({"VectorDimension": "512"}, "dimension", 512),
        ({"DistanceMetric": "euclidean"}, "distanceMetric", "euclidean"),
        (
            {"MetadataConfiguration": {"nonFilterableMetadataKeys": ["X"]}},
            "metadataConfiguration",
            {"nonFilterableMetadataKeys": ["X"]},
        ),
    ],
    ids=["dimension", "distance-metric", "metadata-configuration"],
)
def test_an_update_that_changes_an_index_setting_rebuilds_the_index(
    s3vectors, changed, create_param, expected
):
    new = dict(TEMPLATE_PROPERTIES, **changed)
    _expect_existing(s3vectors)
    s3vectors.stubber.add_response(
        "delete_index", {}, {"vectorBucketName": BUCKET, "indexName": INDEX}
    )
    sent: dict = {}
    s3vectors.stubber.add_response(
        "create_index",
        {
            "indexArn": f"arn:aws:s3vectors:{REGION}:123456789012:bucket/{BUCKET}/index/{INDEX}"
        },
    )
    s3vectors.client.meta.events.register(
        "provide-client-params.s3vectors.CreateIndex",
        lambda params, **_kwargs: sent.update(params),
    )

    assert _update(s3vectors, TEMPLATE_PROPERTIES, new) == "SUCCESS"
    assert s3vectors.calls == ["GetVectorBucket", "DeleteIndex", "CreateIndex"]
    assert sent[create_param] == expected


def test_an_update_that_keeps_the_settings_restores_a_missing_index(s3vectors):
    """An index deleted outside the stack comes back on the next update."""
    _expect_kept_index(s3vectors)
    sent: dict = {}
    s3vectors.client.meta.events.register(
        "provide-client-params.s3vectors.CreateIndex",
        lambda params, **_kwargs: sent.update(params),
    )
    changed = dict(TEMPLATE_PROPERTIES, EmbeddingModel="cohere.embed-english-v3")

    assert _update(s3vectors, TEMPLATE_PROPERTIES, changed) == "SUCCESS"
    assert s3vectors.calls == ["GetVectorBucket", "CreateIndex"]
    assert sent["dimension"] == 1024 and sent["distanceMetric"] == "cosine"


def test_an_update_that_cannot_restore_a_missing_index_fails(s3vectors):
    """Reporting SUCCESS would hand the knowledge base an index ARN that does not exist."""
    _expect_kept_index(
        s3vectors, service_error_code="AccessDeniedException", http_status_code=403
    )
    changed = dict(TEMPLATE_PROPERTIES, EmbeddingModel="cohere.embed-english-v3")

    assert _update(s3vectors, TEMPLATE_PROPERTIES, changed) == "FAILED"


def test_the_kept_index_is_restored_with_the_settings_it_was_created_with(s3vectors):
    """Settings that differ from the handler's defaults reach CreateIndex unchanged."""
    _expect_kept_index(s3vectors)
    sent: dict = {}
    s3vectors.client.meta.events.register(
        "provide-client-params.s3vectors.CreateIndex",
        lambda params, **_kwargs: sent.update(params),
    )
    custom = dict(
        TEMPLATE_PROPERTIES,
        VectorDimension="512",
        DistanceMetric="euclidean",
        MetadataConfiguration={"nonFilterableMetadataKeys": ["X"]},
    )
    changed = dict(custom, EmbeddingModel="cohere.embed-english-v3")

    assert _update(s3vectors, custom, changed) == "SUCCESS"
    assert s3vectors.calls == ["GetVectorBucket", "CreateIndex"]
    assert sent["dimension"] == 512
    assert sent["distanceMetric"] == "euclidean"
    assert sent["metadataConfiguration"] == {"nonFilterableMetadataKeys": ["X"]}
