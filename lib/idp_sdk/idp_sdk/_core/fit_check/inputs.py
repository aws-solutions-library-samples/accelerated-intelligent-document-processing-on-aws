# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reading the samples folder before anything is uploaded.

Every file is either accepted or refused here, and a refusal says why. The routing
question is asked of ``idp_common.ocr.file_types``, the function the OCR step itself
asks first, so a file is refused exactly when the OCR step would not route it by its
extension.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from idp_sdk._core.fit_check.refs import LEGACY_OFFICE, UNSUPPORTED_FORMAT
from idp_sdk._core.fit_check.scripts import count_scripts

#: The formats Discovery reads (docs/discovery.md, "Supported File Types"); WebP is
#: read by multi-document Discovery only.
DISCOVERY_FORMATS = frozenset({"pdf", "png", "jpg", "jpeg", "tiff", "tif"})
MULTI_DOC_DISCOVERY_FORMATS = DISCOVERY_FORMATS | {"webp"}

#: Image formats the OCR step processes as one page.
_IMAGE_FORMATS = frozenset({"jpg", "jpeg", "png", "gif", "bmp", "tiff", "tif", "webp"})

#: Text-layer pages read per PDF when counting scripts; enough to see the script a
#: document is written in without reading a very long file end to end.
_TEXT_LAYER_PAGES = 20


@dataclass
class SampleFile:
    """One file in the samples folder, as found before upload."""

    path: str
    file: str
    format: str
    size_bytes: int
    pages: Optional[int] = None
    refused: Optional[str] = None
    refused_ref: Optional[str] = None
    text_layer_scripts: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _pdf_pages_and_scripts(path: Path) -> tuple[Optional[int], Dict[str, int]]:
    """Page count and text-layer script counts, or ``(None, {})`` if unreadable."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None, {}
    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception:
        return None, {}
    try:
        pages = len(pdf)
        text = []
        for index in range(min(pages, _TEXT_LAYER_PAGES)):
            page = pdf[index]
            textpage = page.get_textpage()
            text.append(textpage.get_text_range())
            textpage.close()
            page.close()
        return pages, count_scripts("\n".join(text))
    except Exception:
        return None, {}
    finally:
        pdf.close()


def inspect_file(path: Path) -> SampleFile:
    """Decide whether one file is accepted, and count what can be counted."""
    from idp_common.ocr.file_types import (
        file_extension,
        is_legacy_ole2_office_file,
        routed_file_type,
    )

    sample = SampleFile(
        path=str(path.resolve()),
        file=path.name,
        format=file_extension(path.name),
        size_bytes=path.stat().st_size,
    )
    routed = routed_file_type(path.name)
    if routed is None:
        shown = f".{sample.format}" if sample.format else "no extension"
        sample.refused = (
            f"{shown} is not a file type the OCR step routes by its extension; "
            "it would be guessed from the content, so it is not uploaded"
        )
        sample.refused_ref = UNSUPPORTED_FORMAT
        return sample
    if routed == "docx":
        with path.open("rb") as handle:
            head = handle.read(8)
        if is_legacy_ole2_office_file(head):
            sample.refused = (
                "a legacy binary Word document (OLE2 container); the OCR step "
                "refuses it with UnsupportedLegacyFormatError. Re-save it as .docx"
            )
            sample.refused_ref = LEGACY_OFFICE
            return sample
    if routed == "pdf":
        sample.pages, sample.text_layer_scripts = _pdf_pages_and_scripts(path)
    elif routed in _IMAGE_FORMATS:
        sample.pages = 1
    return sample


def scan_samples(directory: str) -> List[SampleFile]:
    """Every regular, non-hidden file under ``directory``, in path order.

    Uploads are keyed ``<batch>/<file name>``, so two files with the same name in
    different subfolders would overwrite each other; the second is refused.
    """
    root = Path(directory)
    if not root.is_dir():
        raise NotADirectoryError(f"Samples folder not found: {directory}")
    samples: List[SampleFile] = []
    seen: Dict[str, str] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        sample = inspect_file(path)
        if sample.refused is None and path.name in seen:
            sample.refused = (
                f"another sample has the same file name ({seen[path.name]}), and "
                "uploads are keyed by file name"
            )
        seen.setdefault(path.name, str(relative))
        samples.append(sample)
    return samples
