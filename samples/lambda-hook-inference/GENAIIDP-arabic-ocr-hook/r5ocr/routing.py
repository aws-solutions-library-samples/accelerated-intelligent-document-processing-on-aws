"""Arabic lines take PaddleOCR geometry, English/Latin lines take Textract geometry."""

from __future__ import annotations

from .geometry import Box
from .regions import Region

PREFERENCE = {
    "ar": ("paddle", "textract"),
    "en": ("textract", "paddle"),
}


def choose_box(region: Region, script: str) -> Box | None:
    for source in PREFERENCE.get(script, ()):
        if source in region.boxes:
            return region.boxes[source]
    return None
