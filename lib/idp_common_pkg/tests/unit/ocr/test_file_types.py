# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``routed_file_type`` is the OCR step's by-name routing, asked before an upload."""

import subprocess
import sys

import pytest

from idp_common.ocr.file_types import routed_file_type
from idp_common.ocr.service import OcrService


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.PDF", "pdf"),
        ("a.xls", "xlsx"),
        ("a.doc", "docx"),
        ("a.tif", "tif"),
        ("a.heic", None),
        ("noextension", None),
        ("a.tar.gz", None),
    ],
)
def test_routed_file_type(name, expected):
    assert routed_file_type(name) == expected


@pytest.mark.unit
@pytest.mark.parametrize("name", ["a.pdf", "a.csv", "a.webp", "a.heic", "noext"])
def test_the_ocr_step_routes_by_the_same_function(name):
    """A routed name never reaches the content sniff; an unrouted one always does."""
    content = b"\xff\xfe not utf-8"
    detected = OcrService._detect_file_type(None, name, content)  # type: ignore[arg-type]
    assert detected == (routed_file_type(name) or "pdf")


@pytest.mark.unit
def test_file_types_imports_without_the_ocr_service_dependencies():
    code = (
        "import sys; sys.modules['pypdfium2'] = None; sys.modules['PIL'] = None; "
        "from idp_common.ocr.file_types import routed_file_type, "
        "is_legacy_ole2_office_file; print(routed_file_type('x.pdf'))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "pdf"
