# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Caller-supplied document attributes through the SDK (#1851).

Every upload test runs against moto's S3, and the proof that the attributes
arrive is the ingest side's own reader: ``Document.from_s3_event`` is run over the
object the SDK uploaded, with a real ``head_object`` (and, for the sidecar
transport, a real read of the sidecar). Asserting on the metadata dict alone
would not show the set survives S3's lower-casing and the inline name mapping.
"""

import csv
import json
from unittest.mock import Mock, patch

import pytest
import test_batch_operations_extended as _batch_tests

from idp_common import document_attributes as da
from idp_common.models import Document
from idp_sdk._core.manifest_parser import parse_manifest
from idp_sdk.exceptions import IDPConfigurationError, IDPProcessingError

# The moto stack, its buckets and an IDPClient over them, shared with the batch
# operation tests rather than redefined here.
idp_stack = _batch_tests.idp_stack
client = _batch_tests.client
s3 = _batch_tests.s3
INPUT_BUCKET = _batch_tests.INPUT_BUCKET
OUTPUT_BUCKET = _batch_tests.OUTPUT_BUCKET
SOURCE_BUCKET = _batch_tests.SOURCE_BUCKET
_store_batch = _batch_tests._store_batch


def _intake(key):
    event = {
        "detail": {"bucket": {"name": INPUT_BUCKET}, "object": {"key": key}},
        "time": "2026-11-20T00:00:00Z",
    }
    return Document.from_s3_event(event, OUTPUT_BUCKET)


def _all_keys(s3_client, bucket):
    response = s3_client.list_objects_v2(Bucket=bucket)
    return sorted(obj["Key"] for obj in response.get("Contents", []))


def _pdf(tmp_path, name="a.pdf"):
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4 fake")
    return str(path)


INLINE = {"account_id": "ACME-0042", "region": "eu"}
TYPED = {"case-ref": "C-1", "amount": 12.5, "tags": ["x", "y"], "urgent": True}


@pytest.mark.unit
class TestSingleDocument:
    @pytest.mark.parametrize("attrs", [INLINE, TYPED])
    def test_attributes_reach_intake_intact(self, client, s3, tmp_path, attrs):
        result = client.document.process(_pdf(tmp_path), attributes=dict(attrs))
        doc = _intake(result.document_id)
        assert doc.attributes == attrs
        assert doc.processing_issues == []

    def test_a_typed_set_travels_as_a_sidecar_the_queue_sender_skips(
        self, client, s3, tmp_path
    ):
        result = client.document.process(_pdf(tmp_path), attributes=dict(TYPED))
        sidecar = result.document_id + da.SIDECAR_SUFFIX
        assert sidecar in _all_keys(s3, INPUT_BUCKET)
        assert da.is_sidecar_key(sidecar)

    def test_without_attributes_the_upload_is_unchanged(self, client, s3, tmp_path):
        result = client.document.process(_pdf(tmp_path))
        head = s3.head_object(Bucket=INPUT_BUCKET, Key=result.document_id)
        assert head["Metadata"] == {}
        assert _all_keys(s3, INPUT_BUCKET) == [result.document_id]

    def test_invalid_attributes_are_refused_before_anything_is_uploaded(
        self, client, s3, tmp_path
    ):
        with pytest.raises(IDPConfigurationError, match="'Bad'"):
            client.document.process(_pdf(tmp_path), attributes={"Bad": "x"})
        assert _all_keys(s3, INPUT_BUCKET) == []


@pytest.mark.unit
class TestBatch:
    def test_batch_attributes_apply_to_every_document(self, client, s3, tmp_path):
        _pdf(tmp_path, "a.pdf")
        _pdf(tmp_path, "b.pdf")
        result = client.batch.process(
            directory=str(tmp_path), attributes={"account_id": "ACME-0042"}
        )
        assert len(result.document_ids) == 2
        for key in result.document_ids:
            assert _intake(key).attributes == {"account_id": "ACME-0042"}

    @pytest.mark.parametrize("attrs", [INLINE, TYPED])
    def test_a_copy_from_another_bucket_carries_attributes(self, client, s3, attrs):
        """The S3-URI path copies each object in; the copy carries the caller's
        attributes, by either transport, together with the configuration pin."""
        s3.put_object(Bucket=SOURCE_BUCKET, Key="in/a.pdf", Body=b"%PDF")
        result = client.batch.process(
            s3_uri=f"s3://{SOURCE_BUCKET}/in/",
            config_version="lending",
            attributes=dict(attrs),
        )
        (key,) = result.document_ids
        doc = _intake(key)
        assert doc.attributes == attrs
        assert doc.config_version == "lending"

    @pytest.mark.parametrize("attrs", [INLINE, TYPED])
    def test_a_copy_without_a_configuration_pin_still_carries_attributes(
        self, client, s3, attrs
    ):
        """Without config_version the copy used to keep the source's metadata;
        attributes must replace it, or they would be silently lost."""
        s3.put_object(
            Bucket=SOURCE_BUCKET,
            Key="in/b.pdf",
            Body=b"%PDF",
            Metadata={"unrelated": "source-metadata"},
        )
        result = client.batch.process(
            s3_uri=f"s3://{SOURCE_BUCKET}/in/", attributes=dict(attrs)
        )
        (key,) = result.document_ids
        assert _intake(key).attributes == attrs

    def test_a_manifest_row_overrides_the_batch_set_per_key(self, client, s3, tmp_path):
        a, b = _pdf(tmp_path, "a.pdf"), _pdf(tmp_path, "b.pdf")
        manifest = tmp_path / "m.csv"
        with open(manifest, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["document_path", "attributes"])
            writer.writeheader()
            writer.writerow(
                {
                    "document_path": a,
                    "attributes": json.dumps({"claim": 7, "region": "us"}),
                }
            )
            writer.writerow({"document_path": b, "attributes": ""})
        result = client.batch.process(
            manifest=str(manifest), attributes={"region": "eu", "source": "cli"}
        )
        by_name = {key.rsplit("/", 1)[-1]: key for key in result.document_ids}
        assert _intake(by_name["a.pdf"]).attributes == {
            "claim": 7,
            "region": "us",
            "source": "cli",
        }
        assert _intake(by_name["b.pdf"]).attributes == {"region": "eu", "source": "cli"}

    def test_a_merge_that_breaks_a_bound_refuses_the_batch_before_upload(
        self, client, s3, tmp_path
    ):
        a = _pdf(tmp_path, "a.pdf")
        many = {f"r{i:03d}": "v" for i in range(da.MAX_ATTRIBUTES)}
        manifest = tmp_path / "m.json"
        manifest.write_text(json.dumps([{"document_path": a, "attributes": many}]))
        # Each set alone is within the bound; together they exceed it.
        with pytest.raises(IDPProcessingError, match="more than"):
            client.batch.process(manifest=str(manifest), attributes={"b": "x"})
        assert _all_keys(s3, INPUT_BUCKET) == []

    def test_attributes_on_a_key_already_in_the_input_bucket_are_refused(
        self, client, s3
    ):
        """Such a document was ingested when it was first uploaded, so there is no
        upload left to attach attributes to; refusing beats dropping them."""
        from idp_sdk._core.batch_processor import BatchProcessor

        processor = BatchProcessor(
            stack_name=_batch_tests.STACK_NAME, region=client._region
        )
        doc = {"type": "s3-key", "path": "b/a.pdf", "filename": "a.pdf"}
        with pytest.raises(ValueError, match="already in the input bucket"):
            processor._process_documents([doc], "b", "p", "src", attributes={"a": "b"})

    def test_attributes_with_a_test_set_are_refused(self, client):
        with pytest.raises(IDPConfigurationError, match="test set"):
            client.batch.process(test_set="ts", attributes={"a": "b"})

    def test_invalid_batch_attributes_are_refused_by_type(self, client, tmp_path):
        _pdf(tmp_path)
        with pytest.raises(IDPConfigurationError, match="invalid document attributes"):
            client.batch.process(directory=str(tmp_path), attributes={"A": 1})


@pytest.mark.unit
class TestManifest:
    def test_rows_without_attributes_normalise_exactly_as_before(self, tmp_path):
        a = _pdf(tmp_path)
        manifest = tmp_path / "m.csv"
        manifest.write_text(f"document_path,baseline_source\n{a},\n")
        (row,) = parse_manifest(str(manifest))
        assert row == {
            "path": a,
            "type": "local",
            "filename": "a.pdf",
            "baseline_source": None,
        }

    @pytest.mark.parametrize(
        "cell, match",
        [
            ("{not json", "not valid JSON"),
            ('["a"]', "JSON object"),
            ('{"A": 1}', "'A'"),
        ],
    )
    def test_a_bad_attributes_cell_refuses_the_manifest(self, tmp_path, cell, match):
        a = _pdf(tmp_path)
        manifest = tmp_path / "m.csv"
        with open(manifest, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["document_path", "attributes"])
            writer.writeheader()
            writer.writerow({"document_path": a, "attributes": cell})
        with pytest.raises(ValueError, match=match):
            parse_manifest(str(manifest))


def _monitor_with(entries):
    monitor = Mock()
    monitor.get_batch_status.side_effect = lambda ids: {"completed": entries}
    return monitor


@pytest.mark.unit
class TestResults:
    @patch("idp_sdk._core.progress_monitor.ProgressMonitor")
    def test_get_results_returns_the_documents_attributes(
        self, monitor_cls, client, s3
    ):
        monitor_cls.return_value = _monitor_with(
            [{"document_id": "b/a.pdf", "status": "COMPLETED", "attributes": INLINE}]
        )
        _store_batch(s3, "b", ["b/a.pdf"])
        (document,) = client.batch.get_results("b")["documents"]
        assert document["attributes"] == INLINE

    @patch("idp_sdk._core.progress_monitor.ProgressMonitor")
    def test_a_document_without_attributes_has_no_attributes_key(
        self, monitor_cls, client, s3
    ):
        monitor_cls.return_value = _monitor_with(
            [{"document_id": "b/a.pdf", "status": "COMPLETED"}]
        )
        _store_batch(s3, "b", ["b/a.pdf"])
        (document,) = client.batch.get_results("b")["documents"]
        assert "attributes" not in document
