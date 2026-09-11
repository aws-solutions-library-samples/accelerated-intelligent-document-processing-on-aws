# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A legacy binary `.doc` must fail visibly, not convert to a blank page.

`.doc` was advertised in the upload label, accepted by the picker and routed by
`ocr/service.py` to the `docx` handler for every release that claimed it — but
`convert_word_to_pages` opens the file with python-docx, which reads the OOXML
container only. A legacy OLE2 file raised `PackageNotFoundError`, the handler
swallowed it and returned one empty page reading "Error reading Word document",
and the document then completed classification and extraction with no content.
The failure surfaced downstream as inexplicably empty results — silent data loss,
the same shape as the `.xls` bug in #799 (GitHub #829).

Unlike `.xls`, this cannot be fixed by declaring a dependency: there is no
pure-Python reader for the format. So the document fails with an actionable
message instead, and the upload picker no longer offers the extension.

The fixtures are the real container magic bytes, which is what the detection
keys on — OLE2 (`D0 CF 11 E0 A1 B1 1A E1`) for legacy Office, `PK` for OOXML.
Parsing a genuine `.doc` is not exercised because nothing in the codebase can.
"""

from typing import Any

import pytest

from idp_common.ocr.document_converter import (
    DocumentConverter,
    UnsupportedLegacyFormatError,
    is_legacy_ole2_office_file,
)

pytestmark = pytest.mark.unit

# A legacy .doc/.xls/.ppt is an OLE2 (Compound File Binary) container.
OLE2_HEADER = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512
# .docx/.xlsx are zip containers.
ZIP_HEADER = b"PK\x03\x04" + b"\x00" * 64


class TestTheDetection:
    def test_an_ole2_container_is_recognized(self):
        """Content-based, so it also catches the common case: a .doc renamed
        .docx. Word saves either format and users rename rather than re-save."""
        assert is_legacy_ole2_office_file(OLE2_HEADER) is True

    def test_a_zip_container_is_not(self):
        assert is_legacy_ole2_office_file(ZIP_HEADER) is False

    def test_short_and_empty_input_do_not_raise(self):
        """Sniffing must never be the thing that breaks on a truncated upload."""
        assert is_legacy_ole2_office_file(b"") is False
        assert is_legacy_ole2_office_file(b"\xd0\xcf") is False


class TestTheConverter:
    def test_a_legacy_doc_raises_instead_of_returning_a_blank_page(self):
        with pytest.raises(UnsupportedLegacyFormatError) as raised:
            DocumentConverter(dpi=72).convert_word_to_pages(OLE2_HEADER)

        message = str(raised.value)
        # The message has to tell the uploader what to DO — this is the only place
        # the cause is visible now that the page is not written.
        assert ".docx" in message
        assert "Re-save" in message

    def test_a_corrupt_docx_still_gets_the_error_page(self):
        """The blank-page answer is right for a file that failed to parse; it is
        only wrong for a format that cannot be read at all. This pins that the
        change did not turn every Word failure into a hard failure."""
        pages = DocumentConverter(dpi=72).convert_word_to_pages(ZIP_HEADER)

        assert len(pages) == 1
        assert "Error reading Word document" in pages[0][1]


class TestThroughTheOcrService:
    """The converter raising is only half of it: the service's catch-all used to
    turn every exception into an error page."""

    def _service(self) -> Any:
        from idp_common.ocr.service import OcrService

        # No AWS clients are needed for _process_non_pdf_document.
        return OcrService.__new__(OcrService)

    def test_the_error_is_not_swallowed(self):
        service = self._service()
        service.document_converter = DocumentConverter(dpi=72)

        with pytest.raises(UnsupportedLegacyFormatError):
            service._process_non_pdf_document("docx", OLE2_HEADER)

    def test_other_failures_still_degrade_to_a_page(self):
        service = self._service()
        service.document_converter = DocumentConverter(dpi=72)

        pages = service._process_non_pdf_document("docx", ZIP_HEADER)

        assert len(pages) == 1
        assert "Error" in pages[0][1]
