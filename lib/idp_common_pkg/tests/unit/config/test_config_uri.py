# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Processing a document under a configuration supplied by S3 URI.

`prepare_config_snapshot` is the one gate a supplied configuration passes: it is
read from the input bucket, validated with `validate_config`, merged onto the
system defaults and written to the working bucket under a content-addressed key.
`load_config_snapshot` (reached through `get_config(config_uri=...)`) is what
every later step reads. Nothing here touches the configuration table.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
import yaml
from botocore.exceptions import ClientError
from moto import mock_aws

from idp_common.config import (
    SNAPSHOT_PREFIX,
    ConfigUriError,
    get_config,
    load_config_snapshot,
    prepare_config_snapshot,
)
from idp_common.config.models import IDPConfig
from idp_common.models import Document

INPUT = "config-uri-input"
WORKING = "config-uri-working"

SUPPLIED = {
    "classes": [
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "Invoice",
            "type": "object",
            "x-aws-idp-document-type": "Invoice",
            "description": "A commercial invoice",
            "properties": {
                "InvoiceNumber": {"type": "string", "description": "Invoice number"}
            },
        }
    ],
    "extraction": {"temperature": 0.25},
}


@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=INPUT)
        client.create_bucket(Bucket=WORKING)
        yield client


def _put(s3, key, body):
    s3.put_object(Bucket=INPUT, Key=key, Body=body)
    return f"s3://{INPUT}/{key}"


def _stage(s3, uri):
    return prepare_config_snapshot(
        uri, allowed_bucket=INPUT, working_bucket=WORKING, s3_client=s3
    )


@pytest.mark.unit
class TestSnapshot:
    def test_a_valid_configuration_is_snapshotted_merged_onto_the_defaults(self, s3):
        snapshot = _stage(s3, _put(s3, "configs/invoice.json", json.dumps(SUPPLIED)))

        assert snapshot.startswith(f"s3://{WORKING}/{SNAPSHOT_PREFIX}/")
        config = load_config_snapshot(snapshot)
        assert isinstance(config, IDPConfig)
        assert config.extraction.temperature == 0.25
        assert [c["$id"] for c in config.classes] == ["Invoice"]
        # Merged: a section the caller never wrote carries the system default.
        assert config.classification.model

    def test_the_snapshot_key_is_content_addressed(self, s3):
        """A retried invocation that stages the same configuration again must write
        the same object, not accumulate copies."""
        body = json.dumps(SUPPLIED)
        first = _stage(s3, _put(s3, "a.json", body))
        second = _stage(s3, _put(s3, "b.json", body))
        assert first == second

    def test_the_snapshot_does_not_follow_later_edits_to_the_source(self, s3):
        uri = _put(s3, "configs/invoice.json", json.dumps(SUPPLIED))
        snapshot = _stage(s3, uri)
        edited = {**SUPPLIED, "extraction": {"temperature": 0.9}}
        _put(s3, "configs/invoice.json", json.dumps(edited))

        assert load_config_snapshot(snapshot).extraction.temperature == 0.25

    def test_yaml_is_accepted_by_extension(self, s3):
        snapshot = _stage(
            s3, _put(s3, "configs/invoice.yaml", yaml.safe_dump(SUPPLIED))
        )
        assert load_config_snapshot(snapshot).extraction.temperature == 0.25

    def test_a_legacy_class_list_is_migrated_like_a_stored_profile(self, s3):
        """A stored profile's legacy `attributes` list is migrated when it is read;
        a supplied one must be too, or it validates and is then misread."""
        legacy = {
            "classes": [
                {
                    "name": "Invoice",
                    "description": "A commercial invoice",
                    "attributes": [
                        {"name": "InvoiceNumber", "description": "Invoice number"}
                    ],
                }
            ]
        }
        snapshot = _stage(s3, _put(s3, "legacy.json", json.dumps(legacy)))
        (invoice,) = load_config_snapshot(snapshot).classes
        assert "properties" in invoice
        assert "InvoiceNumber" in invoice["properties"]

    def test_as_model_false_returns_a_dict(self, s3):
        snapshot = _stage(s3, _put(s3, "c.json", json.dumps(SUPPLIED)))
        loaded = load_config_snapshot(snapshot, as_model=False)
        assert isinstance(loaded, dict)
        assert loaded["extraction"]["temperature"] == 0.25


@pytest.mark.unit
class TestRejection:
    @pytest.mark.parametrize(
        ("key", "body", "reason"),
        [
            ("bad.json", "{not json", "could not be parsed"),
            ("list.json", "[1, 2]", "must be an object"),
            ("bad.yaml", "a: [unclosed", "could not be parsed"),
            (
                "dpi.json",
                json.dumps({**SUPPLIED, "ocr": {"image": {"dpi": "abc"}}}),
                "failed validation",
            ),
            (
                "bda.json",
                json.dumps({**SUPPLIED, "use_bda": True}),
                "use_bda: true",
            ),
        ],
    )
    def test_an_unusable_configuration_is_rejected(self, s3, key, body, reason):
        with pytest.raises(ConfigUriError, match=reason):
            _stage(s3, _put(s3, key, body))
        assert s3.list_objects_v2(Bucket=WORKING).get("KeyCount", 0) == 0

    def test_a_configuration_outside_the_input_bucket_is_rejected(self, s3):
        with pytest.raises(ConfigUriError, match="must name an object in the input"):
            _stage(s3, f"s3://{WORKING}/anything.json")

    def test_a_missing_object_is_rejected(self, s3):
        with pytest.raises(ConfigUriError, match="NoSuchKey"):
            _stage(s3, f"s3://{INPUT}/missing.json")

    def test_a_non_s3_uri_is_rejected(self, s3):
        with pytest.raises(ConfigUriError, match="Invalid S3 URI"):
            _stage(s3, "https://example.com/config.json")

    def test_a_transient_s3_error_is_not_a_rejection(self):
        """The caller retries a ClientError; a ConfigUriError would fail the
        document for a throttle."""
        client = MagicMock()
        client.get_object.side_effect = ClientError(
            {"Error": {"Code": "SlowDown", "Message": "slow down"}}, "GetObject"
        )
        with pytest.raises(ClientError):
            prepare_config_snapshot(
                f"s3://{INPUT}/c.json",
                allowed_bucket=INPUT,
                working_bucket=WORKING,
                s3_client=client,
            )


@pytest.mark.unit
class TestGetConfig:
    def test_config_uri_never_reads_the_configuration_table(self, s3):
        snapshot = _stage(s3, _put(s3, "c.json", json.dumps(SUPPLIED)))
        with patch(
            "idp_common.config.ConfigurationReader",
            side_effect=AssertionError("the configuration table was read"),
        ):
            config = get_config(
                as_model=True, version="some-profile", revision=4, config_uri=snapshot
            )
        assert config.extraction.temperature == 0.25


def _event(key="invoice.pdf"):
    return {
        "detail": {"bucket": {"name": INPUT}, "object": {"key": key}},
        "time": "2026-09-30T12:00:00Z",
    }


@pytest.mark.unit
class TestDocumentCarriesConfigUri:
    def test_from_s3_event_reads_config_uri(self):
        head = {"Metadata": {"config-uri": f"s3://{INPUT}/configs/invoice.json"}}
        with patch("boto3.client") as mock_client:
            mock_client.return_value.head_object.return_value = head
            doc = Document.from_s3_event(_event(), "output-bucket")
        assert doc.config_uri == f"s3://{INPUT}/configs/invoice.json"
        assert doc.config_version is None

    def test_config_uri_wins_over_config_version(self):
        """A profile name next to a supplied configuration would label the
        document with a configuration it was not processed under — and
        config_version is what the RBAC scope checks compare."""
        head = {
            "Metadata": {
                "config-uri": f"s3://{INPUT}/configs/invoice.json",
                "config-version": "lending",
                "config-revision": "3",
            }
        }
        with patch("boto3.client") as mock_client:
            mock_client.return_value.head_object.return_value = head
            doc = Document.from_s3_event(_event(), "output-bucket")
        assert doc.config_uri == f"s3://{INPUT}/configs/invoice.json"
        assert doc.config_version is None
        assert doc.config_revision is None

    def test_an_ordinary_upload_has_no_config_uri(self):
        with patch("boto3.client") as mock_client:
            mock_client.return_value.head_object.return_value = {"Metadata": {}}
            doc = Document.from_s3_event(_event(), "output-bucket")
        assert doc.config_uri is None

    def test_config_uri_survives_every_step_boundary(self, s3):
        """to_dict/from_dict for the full document, and the compressed wrapper
        the pipeline-hooks dispatcher reads without decompressing."""
        snapshot = f"s3://{WORKING}/{SNAPSHOT_PREFIX}/abc.json"
        doc = Document(id="invoice.pdf", input_key="invoice.pdf", config_uri=snapshot)

        assert Document.from_dict(doc.to_dict()).config_uri == snapshot
        wrapper = doc.compress(WORKING, "test")
        assert wrapper["config_uri"] == snapshot
        assert Document.decompress(WORKING, wrapper).config_uri == snapshot
