# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Load PDF pages so that filled-in form values appear in the rendered image."""

from typing import Any


def load_page_for_render(pdf_document: Any, page_index: int) -> Any:
    """Return page ``page_index`` of ``pdf_document`` ready for ``render()``.

    ``pdf_document`` must be a ``pypdfium2.PdfDocument`` on which
    ``init_forms()`` has already been called, so that AcroForm field values are
    drawn even when the stored appearance stream is empty (issue #240).

    A page with a form environment is flattened, which merges its form fields
    and annotations into the page content. Flattening leaves the loaded page
    handle stale: the annotations are gone from it but the merged content is not
    yet parsed, so rendering that handle loses every value held in an annotation
    such as FreeText (issue #1780). The page is therefore closed and loaded
    again after flattening. The caller owns the returned page.
    """
    page = pdf_document[page_index]
    if page.formenv is None:
        return page
    page.flatten()
    page.close()
    return pdf_document[page_index]
