# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Updated tests for manifest parser module matching new S3 URI support
"""

import csv
import json

import pytest

from idp_sdk._core.manifest_parser import (
    ManifestParser,
    parse_manifest,
    validate_manifest,
)


class TestManifestParser:
    """Test manifest parsing functionality"""

    def test_csv_parsing_basic_s3(self, tmp_path):
        """Test basic CSV manifest parsing with S3 URIs"""
        manifest_file = tmp_path / "test.csv"

        with open(manifest_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["document_path", "baseline_source"])
            writer.writerow(["s3://bucket/doc1.pdf", "doc1"])
            writer.writerow(["s3://bucket/doc2.pdf", "doc2"])

        parser = ManifestParser(str(manifest_file))
        documents = parser.parse()

        assert len(documents) == 2
        assert documents[0]["path"] == "s3://bucket/doc1.pdf"
        assert documents[0]["type"] == "s3"
        assert documents[0]["filename"] == "doc1.pdf"

    def test_csv_parsing_local(self, tmp_path):
        """Test CSV parsing with local files"""
        manifest_file = tmp_path / "test.csv"

        # Create local test files
        doc1 = tmp_path / "doc1.pdf"
        doc1.write_text("test")

        with open(manifest_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["document_path"])
            writer.writerow([str(doc1)])

        parser = ManifestParser(str(manifest_file))
        documents = parser.parse()

        assert len(documents) == 1
        assert documents[0]["type"] == "local"

    def test_csv_auto_generate_id(self, tmp_path):
        """Test auto-generation of document ID from filename"""
        manifest_file = tmp_path / "test.csv"

        with open(manifest_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["document_path"])
            writer.writerow(["s3://bucket/folder/document-name.pdf"])

        parser = ManifestParser(str(manifest_file))
        documents = parser.parse()

        assert documents[0]["filename"] == "document-name.pdf"

    def test_json_parsing_array_format(self, tmp_path):
        """Test JSON parsing with array format"""
        manifest_file = tmp_path / "test.json"

        data = [
            {"document_path": "s3://bucket/doc1.pdf", "baseline_source": "doc1"},
            {"document_path": "s3://bucket/doc2.pdf", "baseline_source": "doc2"},
        ]

        with open(manifest_file, "w") as f:
            json.dump(data, f)

        parser = ManifestParser(str(manifest_file))
        documents = parser.parse()

        assert len(documents) == 2
        assert documents[0]["type"] == "s3"

    def test_missing_document_path(self, tmp_path):
        """Test error handling for missing document path"""
        manifest_file = tmp_path / "test.csv"

        with open(manifest_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["baseline_source"])
            writer.writerow(["doc1"])

        parser = ManifestParser(str(manifest_file))

        with pytest.raises(ValueError, match="Missing required field"):
            parser.parse()

    def test_local_file_not_found(self, tmp_path):
        """Test error handling for missing local file"""
        manifest_file = tmp_path / "test.csv"

        with open(manifest_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["document_path"])
            writer.writerow(["/nonexistent/file.pdf"])

        parser = ManifestParser(str(manifest_file))

        with pytest.raises(ValueError, match="Local file not found"):
            parser.parse()

    def test_s3_uri_support(self, tmp_path):
        """Test that S3 URIs are now supported"""
        manifest_file = tmp_path / "test.csv"

        with open(manifest_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["document_path"])
            writer.writerow(["s3://bucket/key.pdf"])

        parser = ManifestParser(str(manifest_file))
        documents = parser.parse()

        assert len(documents) == 1
        assert documents[0]["type"] == "s3"
        assert documents[0]["path"] == "s3://bucket/key.pdf"

    def test_validate_manifest_success(self, tmp_path):
        """Test manifest validation success"""
        manifest_file = tmp_path / "test.csv"

        with open(manifest_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["document_path"])
            writer.writerow(["s3://bucket/doc1.pdf"])

        is_valid, error = validate_manifest(str(manifest_file))

        assert is_valid
        assert error is None

    def test_validate_manifest_duplicate_filenames(self, tmp_path):
        """Test validation catches duplicate filenames"""
        manifest_file = tmp_path / "test.csv"

        with open(manifest_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["document_path", "baseline_source"])
            writer.writerow(["s3://bucket/folder1/invoice.pdf", "doc1"])
            writer.writerow(["s3://bucket/folder2/invoice.pdf", "doc2"])

        is_valid, error = validate_manifest(str(manifest_file))

        assert not is_valid
        assert error is not None
        assert "Duplicate filenames" in error

    def test_validate_manifest_empty(self, tmp_path):
        """Test validation catches empty manifests"""
        manifest_file = tmp_path / "test.csv"

        with open(manifest_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["document_path"])
            # No data rows

        is_valid, error = validate_manifest(str(manifest_file))

        assert not is_valid
        assert error is not None
        assert "no documents" in error

    def test_unsupported_format(self, tmp_path):
        """Test error for unsupported file format"""
        manifest_file = tmp_path / "test.xml"
        manifest_file.write_text("<manifest></manifest>")

        with pytest.raises(ValueError, match="Unsupported manifest format"):
            ManifestParser(str(manifest_file))

    def test_baseline_source_field(self, tmp_path):
        """Test that baseline_source field is parsed"""
        manifest_file = tmp_path / "test.csv"

        with open(manifest_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["document_path", "baseline_source"])
            writer.writerow(["s3://bucket/doc.pdf", "s3://baselines/doc1/"])

        parser = ManifestParser(str(manifest_file))
        documents = parser.parse()

        assert documents[0]["baseline_source"] == "s3://baselines/doc1/"

    def test_parse_manifest_convenience_function(self, tmp_path):
        """Test convenience parse_manifest function"""
        manifest_file = tmp_path / "test.csv"

        with open(manifest_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["document_path"])
            writer.writerow(["s3://bucket/doc1.pdf"])

        documents = parse_manifest(str(manifest_file))

        assert len(documents) == 1
