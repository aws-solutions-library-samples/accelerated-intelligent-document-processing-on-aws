# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which file types the OCR step routes, answerable without the OCR service.

``OcrService._detect_file_type`` decides a file's type by its extension first and
by its content only when the extension is not one of these. That first decision
lives here so that a caller deciding, before an upload, whether a file will be
routed by name (the SDK's fit check) asks the same function the OCR step does,
and imports nothing but the standard library to do it.

``scripts/check_solution_profile.py`` reads the extension vocabulary from
:func:`routed_file_type`'s source, so a format added here is one the solution
profile has to describe.
"""

from __future__ import annotations

from typing import Optional

# Pre-OOXML Office files are OLE2/CFB containers ("D0 CF 11 E0 A1 B1 1A E1"),
# while .docx/.xlsx are zip containers ("PK"). Sniffing the container rather
# than trusting the extension also catches a .doc that was renamed .docx —
# which is how these files usually arrive, since Word will happily save either.
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def is_legacy_ole2_office_file(content: bytes) -> bool:
    """True if ``content`` is a pre-OOXML (OLE2/CFB) Office document."""
    return content[: len(_OLE2_MAGIC)] == _OLE2_MAGIC


def file_extension(filename: str) -> str:
    """The lower-cased text after the last ``.``, or ``""`` when there is none."""
    return filename.lower().split(".")[-1] if "." in filename else ""


def routed_file_type(filename: str) -> Optional[str]:
    """The file type the OCR step assigns ``filename`` by its extension alone.

    ``None`` means the extension is not one the OCR step routes by name; the OCR
    step then guesses from the content (a PDF header, a zip naming ``xl/`` or
    ``word/``, UTF-8 text, and otherwise PDF).
    """
    ext = file_extension(filename)
    if ext == "txt":
        return "txt"
    elif ext == "csv":
        return "csv"
    elif ext in ["xlsx", "xls"]:
        return "xlsx"
    elif ext in ["docx", "doc"]:
        return "docx"
    elif ext in ["pdf"]:
        return "pdf"
    elif ext in ["jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "webp"]:
        return ext
    return None
