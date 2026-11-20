# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
OCR module for IDP Common Package.

Provides a service for processing PDF documents with AWS Textract.

``OcrService`` is imported on first use rather than here, so that
``idp_common.ocr.file_types`` can be imported without the OCR service's
dependencies (pypdfium2, Pillow).
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from idp_common.ocr.service import OcrService

__all__ = ["OcrService"]


def __getattr__(name: str) -> Any:
    if name == "OcrService":
        from idp_common.ocr.service import OcrService

        return OcrService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
