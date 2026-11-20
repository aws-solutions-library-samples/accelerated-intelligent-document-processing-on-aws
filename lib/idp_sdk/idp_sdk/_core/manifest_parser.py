# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Manifest Parser Module

Parses CSV and JSON manifest files containing document batch information.
"""

import csv
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class ManifestParser:
    """Parses manifest files in CSV or JSON format"""

    def __init__(self, manifest_path: str):
        """
        Initialize manifest parser

        Args:
            manifest_path: Path to manifest file (CSV or JSON)
        """
        self.manifest_path = manifest_path
        self.format = self._detect_format()

    def _detect_format(self) -> str:
        """Detect manifest format from file extension"""
        ext = Path(self.manifest_path).suffix.lower()

        if ext in [".csv", ".txt"]:
            return "csv"
        elif ext in [".json", ".jsonl"]:
            return "json"
        else:
            raise ValueError(f"Unsupported manifest format: {ext}. Use .csv or .json")

    def parse(self) -> List[Dict]:
        """
        Parse manifest file and return list of document specifications

        Returns:
            List of document dictionaries with keys:
                - document_id: Unique identifier
                - path: Local file path or S3 key
                - type: 'local' or 's3-key'
        """
        logger.info(f"Parsing {self.format.upper()} manifest: {self.manifest_path}")

        if self.format == "csv":
            return self._parse_csv()
        elif self.format == "json":
            return self._parse_json()
        else:
            raise ValueError(f"Unsupported format: {self.format}")

    def _parse_csv(self) -> List[Dict]:
        """Parse CSV manifest"""
        documents = []

        with open(self.manifest_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)

            for row_num, row in enumerate(reader, start=2):  # Start at 2 (after header)
                try:
                    doc = self._validate_and_normalize_row(row, row_num)
                    documents.append(doc)
                except ValueError as e:
                    logger.error(f"Row {row_num}: {e}")
                    raise

        logger.info(f"Parsed {len(documents)} documents from CSV")
        return documents

    def _parse_json(self) -> List[Dict]:
        """Parse JSON manifest"""
        with open(self.manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Handle both array format and object with 'documents' key
        if isinstance(data, list):
            documents_list = data
        elif isinstance(data, dict) and "documents" in data:
            documents_list = data["documents"]
        else:
            raise ValueError(
                "JSON manifest must be an array or object with 'documents' key"
            )

        documents = []
        for idx, doc in enumerate(documents_list, start=1):
            try:
                validated_doc = self._validate_and_normalize_row(doc, idx)
                documents.append(validated_doc)
            except ValueError as e:
                logger.error(f"Document {idx}: {e}")
                raise

        logger.info(f"Parsed {len(documents)} documents from JSON")
        return documents

    def _validate_and_normalize_row(self, row: Dict, row_num: int) -> Dict:
        """
        Validate and normalize a manifest row

        Args:
            row: Raw row data
            row_num: Row number for error messages

        Returns:
            Normalized document dictionary with keys:
                - document_id: Unique identifier
                - path: Local file path or full S3 URI
                - type: 'local' or 's3' (auto-detected)
                - filename: Base filename
        """
        # Required field: document_path
        document_path = row.get("document_path") or row.get("path", "").strip()

        if not document_path:
            raise ValueError("Missing required field 'document_path' or 'path'")

        # Auto-detect type based on path format
        if document_path.startswith("s3://"):
            doc_type = "s3"
            # Validate S3 URI format
            if len(document_path) < 8 or "/" not in document_path[5:]:
                raise ValueError(f"Invalid S3 URI format: {document_path}")
            filename = os.path.basename(document_path)
        elif os.path.isabs(document_path) or os.path.exists(document_path):
            doc_type = "local"
            # Validate local file exists
            if not os.path.exists(document_path):
                raise ValueError(f"Local file not found: {document_path}")
            filename = os.path.basename(document_path)
        else:
            raise ValueError(
                f"Invalid path '{document_path}'. Use absolute local path or s3:// URI"
            )

        # Get baseline_source (optional)
        baseline_source = row.get("baseline_source", "").strip() or None

        normalized = {
            "path": document_path,
            "type": doc_type,
            "filename": filename,
            "baseline_source": baseline_source,
        }

        # Optional caller-supplied document attributes (#1851): a JSON object, or
        # in a CSV a cell holding one. Validated here so a bad row refuses the
        # manifest before anything is uploaded. The key is added only when the
        # row carries attributes, so other rows normalise exactly as before.
        attributes = self._row_attributes(row.get("attributes"))
        if attributes:
            normalized["attributes"] = attributes

        return normalized

    @staticmethod
    def _row_attributes(raw) -> Optional[Dict]:
        """A row's ``attributes`` value, validated, or None when it has none."""
        from idp_sdk._core.document_attributes import validate

        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError as e:
                raise ValueError(f"'attributes' is not valid JSON: {e}") from e
        return validate(raw) or None


def parse_manifest(manifest_path: str) -> List[Dict]:
    """
    Convenience function to parse a manifest file

    Args:
        manifest_path: Path to manifest file

    Returns:
        List of document dictionaries
    """
    parser = ManifestParser(manifest_path)
    return parser.parse()


def validate_manifest(manifest_path: str) -> tuple[bool, Optional[str]]:
    """
    Validate a manifest file without fully parsing it

    Args:
        manifest_path: Path to manifest file

    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        parser = ManifestParser(manifest_path)
        documents = parser.parse()

        if not documents:
            return False, "Manifest contains no documents"

        # Check for duplicate filenames (which would cause S3 key collisions)
        filenames = [doc["filename"] for doc in documents]
        if len(filenames) != len(set(filenames)):
            duplicates = [f for f in filenames if filenames.count(f) > 1]
            return (
                False,
                f"Duplicate filenames found: {', '.join(set(duplicates))}.",
            )

        return True, None

    except Exception as e:
        return False, str(e)
