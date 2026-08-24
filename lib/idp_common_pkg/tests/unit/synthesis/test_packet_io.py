# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for synthesis.packet_io (read generator packet + field-name gate)."""

import json
import os
from unittest.mock import Mock

import pytest

from idp_common.synthesis import packet_io

pytestmark = pytest.mark.unit


def _make_packet(root, pdf_name, sections):
    input_dir = os.path.join(root, "input")
    os.makedirs(input_dir, exist_ok=True)
    with open(os.path.join(input_dir, pdf_name), "wb") as fh:
        fh.write(b"%PDF-1.4 fake")
    for i, section in enumerate(sections, start=1):
        sect_dir = os.path.join(root, "baseline", pdf_name, "sections", str(i))
        os.makedirs(sect_dir, exist_ok=True)
        with open(os.path.join(sect_dir, "result.json"), "w") as fh:
            json.dump(section, fh)


SECTION = {
    "document_class": {"type": "Paystub"},
    "split_document": {"page_indices": [0, 1]},
    "inference_result": {
        "EmployeeName": "Jane Doe",
        "GrossPay": 5000,
        "Address": {"City": "Seattle", "ZIP Code": "98101"},
        "Earnings": [{"Type": "Regular", "Amount": 4000}],
    },
}


class TestReadPacket:
    def test_reads_input_and_sections(self, tmp_path):
        root = str(tmp_path)
        _make_packet(root, "packet_001.pdf", [SECTION])
        docs = packet_io.read_packet(root)
        assert len(docs) == 1
        assert docs[0].pdf_path.endswith("packet_001.pdf")
        assert docs[0].sections[0]["document_class"]["type"] == "Paystub"

    def test_sections_sorted_numerically(self, tmp_path):
        root = str(tmp_path)
        _make_packet(root, "p.pdf", [dict(SECTION), dict(SECTION), dict(SECTION)])
        docs = packet_io.read_packet(root)
        assert len(docs[0].sections) == 3

    def test_missing_input_dir_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            packet_io.read_packet(str(tmp_path))


class TestValidateFieldNames:
    def test_passes_when_keys_subset(self, tmp_path):
        root = str(tmp_path)
        _make_packet(root, "p.pdf", [SECTION])
        docs = packet_io.read_packet(root)
        allowed = {"EmployeeName", "GrossPay", "City", "ZIP Code", "Type", "Amount"}
        result = packet_io.validate_field_names(docs, allowed)
        assert result.ok
        assert result.checked_documents == 1
        assert result.extra_keys == set()

    def test_flags_drifted_keys(self, tmp_path):
        root = str(tmp_path)
        _make_packet(root, "p.pdf", [SECTION])
        docs = packet_io.read_packet(root)
        allowed = {"EmployeeName"}
        result = packet_io.validate_field_names(docs, allowed)
        assert not result.ok
        assert "GrossPay" in result.extra_keys
        assert "ZIP Code" in result.extra_keys


class TestPruneToAllowedFields:
    def test_drops_extra_leaf_keys_keeps_schema_keys(self, tmp_path):
        root = str(tmp_path)
        _make_packet(root, "p.pdf", [json.loads(json.dumps(SECTION))])
        docs = packet_io.read_packet(root)
        allowed = {"EmployeeName", "City", "ZIP Code", "Type", "Amount"}
        removed = packet_io.prune_documents_to_allowed_fields(docs, allowed)
        assert removed == 1  # GrossPay dropped
        inf = docs[0].sections[0]["inference_result"]
        assert "GrossPay" not in inf
        assert inf["EmployeeName"] == "Jane Doe"
        assert inf["Address"] == {"City": "Seattle", "ZIP Code": "98101"}
        assert inf["Earnings"] == [{"Type": "Regular", "Amount": 4000}]

    def test_pruned_then_passes_validation(self, tmp_path):
        root = str(tmp_path)
        _make_packet(root, "p.pdf", [json.loads(json.dumps(SECTION))])
        docs = packet_io.read_packet(root)
        allowed = {"EmployeeName", "GrossPay", "City", "ZIP Code", "Type", "Amount"}
        # generator added two extra YTD-style fields not in the schema
        docs[0].sections[0]["inference_result"]["ytd_medicare"] = 12
        docs[0].sections[0]["inference_result"]["ytd_social_security"] = 34
        packet_io.prune_documents_to_allowed_fields(docs, allowed)
        assert packet_io.validate_field_names(docs, allowed).ok


class TestUploadPacketToTestSet:
    def test_uploads_input_and_baseline_keys(self, tmp_path):
        root = str(tmp_path)
        _make_packet(root, "packet_001.pdf", [SECTION])
        docs = packet_io.read_packet(root)
        s3 = Mock()
        count = packet_io.upload_packet_to_test_set(
            docs, "my-test-set", "test-set-bucket", s3_client=s3
        )
        assert count == 1
        s3.upload_file.assert_called_once()
        input_key = s3.upload_file.call_args[0][2]
        assert input_key == "my-test-set/input/packet_001.pdf"
        baseline_key = s3.put_object.call_args.kwargs["Key"]
        assert baseline_key == (
            "my-test-set/baseline/packet_001.pdf/sections/1/result.json"
        )

    def test_name_prefix_makes_keys_unique(self, tmp_path):
        root = str(tmp_path)
        _make_packet(root, "packet_001.pdf", [SECTION])
        docs = packet_io.read_packet(root)
        s3 = Mock()
        count = packet_io.upload_packet_to_test_set(
            docs, "my-test-set", "test-set-bucket", s3_client=s3, name_prefix="abc123_"
        )
        assert count == 1
        input_key = s3.upload_file.call_args[0][2]
        assert input_key == "my-test-set/input/abc123_packet_001.pdf"
        baseline_key = s3.put_object.call_args.kwargs["Key"]
        assert baseline_key == (
            "my-test-set/baseline/abc123_packet_001.pdf/sections/1/result.json"
        )

    def test_writes_synthetic_source_marker(self, tmp_path):
        root = str(tmp_path)
        _make_packet(root, "packet_001.pdf", [SECTION])
        docs = packet_io.read_packet(root)
        s3 = Mock()
        packet_io.upload_packet_to_test_set(
            docs, "my-test-set", "test-set-bucket", s3_client=s3
        )
        # A '.source' marker with body 'synthetic' must be written so the API's
        # test-set discovery can tag generated sets as synthetic.
        marker_calls = [
            c
            for c in s3.put_object.call_args_list
            if c.kwargs.get("Key") == "my-test-set/.source"
        ]
        assert len(marker_calls) == 1
        assert marker_calls[0].kwargs["Body"] == b"synthetic"

    def test_uploads_pdf_with_an_explicit_content_type(self, tmp_path):
        """Regression: upload_file defaults to binary/octet-stream.

        A browser handed octet-stream downloads the file instead of rendering it,
        so "View source document" silently became a download for every generated
        test set while zip-uploaded sets worked.
        """
        root = str(tmp_path)
        _make_packet(root, "packet_001.pdf", [SECTION])
        docs = packet_io.read_packet(root)
        s3 = Mock()

        packet_io.upload_packet_to_test_set(
            docs, "my-test-set", "test-set-bucket", s3_client=s3
        )

        extra = s3.upload_file.call_args.kwargs.get("ExtraArgs") or {}
        assert extra.get("ContentType") == "application/pdf"
