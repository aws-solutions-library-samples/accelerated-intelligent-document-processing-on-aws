# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Build small PDFs covering each way a filled-in form can carry its values.

Every filled-in value is drawn in pure blue and everything else on the page in
black, so a test can tell whether the values reached the rendered image by
counting blue pixels inside the value's rectangle. The PDFs are written byte by
byte rather than through a PDF library so that each one has exactly the
structure it is named for and nothing a library would add on its own.

The shapes, and the rendering sequence that has been measured to lose each one:

- ``plain``: static page text, no annotations, no AcroForm.
- ``widget_needs_appearance``: an AcroForm text field whose value is only in
  ``/V``, with an empty appearance stream and ``/NeedAppearances true``. It
  renders blank unless the form engine is initialised (issue #240).
- ``widget_with_appearance``: a checked checkbox whose ``/AS`` appearance draws
  the check mark, so it renders without the form engine.
- ``freetext_with_acroform``: values in FreeText annotations with complete
  appearance streams, in a document that also has an AcroForm dictionary. It
  renders blank when the page is flattened and the stale page handle is
  rendered (issue #1780).
- ``freetext_without_acroform``: the same annotations with no AcroForm, so no
  form environment exists and nothing is flattened.
"""

from typing import Callable, Dict, List, Tuple

PAGE_WIDTH = 300
PAGE_HEIGHT = 200

# Rectangle (x0, y0, x1, y1) in PDF points, bottom-left origin, holding the
# blue value in every shape that has one.
VALUE_RECT = (40, 100, 260, 130)

_BLUE = "0 0 1 rg"
_HELV = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"


def _stream(body: str, extra: str = "") -> str:
    data = body.encode("latin-1")
    return f"<< /Length {len(data)} {extra}>>\nstream\n{body}\nendstream"


def _assemble(objects: List[str]) -> bytes:
    """Number objects from 1 in list order and write a correct xref table."""
    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def _rect() -> str:
    return " ".join(str(v) for v in VALUE_RECT)


def _bbox() -> str:
    x0, y0, x1, y1 = VALUE_RECT
    return f"0 0 {x1 - x0} {y1 - y0}"


def _value_appearance(text: str) -> str:
    """A form XObject drawing ``text`` in blue, sized to VALUE_RECT."""
    body = f"{_BLUE} BT /Helv 18 Tf 4 8 Td ({text}) Tj ET"
    return _stream(
        body,
        f"/Type /XObject /Subtype /Form /BBox [{_bbox()}] "
        f"/Resources << /Font << /Helv 7 0 R >> >> ",
    )


def _page_content() -> str:
    return _stream("0 0 0 rg BT /Helv 12 Tf 40 160 Td (Label:) Tj ET")


def _build(annot: str | None, appearance: str | None, acroform: str | None) -> bytes:
    """Lay out the fixed object numbers every shape shares.

    1 catalog, 2 pages, 3 page, 4 page content, 5 annotation, 6 its appearance,
    7 Helvetica, 8 a second appearance (unused unless the annotation names it).
    """
    annots = "/Annots [5 0 R]" if annot else ""
    catalog = "<< /Type /Catalog /Pages 2 0 R"
    if acroform:
        catalog += f" /AcroForm {acroform}"
    catalog += " >>"
    objects = [
        catalog,
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
        f"/Contents 4 0 R /Resources << /Font << /Helv 7 0 R >> >> {annots} >>",
        _page_content(),
        annot or "null",
        appearance or "null",
        _HELV,
        "null",
    ]
    return _assemble(objects)


def plain() -> bytes:
    return _build(None, None, None)


def widget_needs_appearance() -> bytes:
    annot = (
        f"<< /Type /Annot /Subtype /Widget /FT /Tx /T (name) /V (Jack) "
        f"/Rect [{_rect()}] /P 3 0 R /F 4 /DA (/Helv 18 Tf {_BLUE}) "
        f"/AP << /N 6 0 R >> >>"
    )
    empty = _stream(
        "", f"/Type /XObject /Subtype /Form /BBox [{_bbox()}] /Resources << >> "
    )
    acroform = (
        "<< /Fields [5 0 R] /NeedAppearances true /DA (/Helv 0 Tf 0 g) "
        "/DR << /Font << /Helv 7 0 R >> >> >>"
    )
    return _build(annot, empty, acroform)


def widget_with_appearance() -> bytes:
    x0, y0, x1, y1 = VALUE_RECT
    check = _stream(
        f"{_BLUE} 0 0 {x1 - x0} {y1 - y0} re f",
        f"/Type /XObject /Subtype /Form /BBox [{_bbox()}] /Resources << >> ",
    )
    annot = (
        f"<< /Type /Annot /Subtype /Widget /FT /Btn /T (agree) /V /Yes /AS /Yes "
        f"/Rect [{_rect()}] /P 3 0 R /F 4 /AP << /N << /Yes 6 0 R >> >> >>"
    )
    acroform = "<< /Fields [5 0 R] >>"
    return _build(annot, check, acroform)


def _freetext() -> str:
    return (
        f"<< /Type /Annot /Subtype /FreeText /Rect [{_rect()}] /P 3 0 R /F 4 "
        f"/Contents (N/A) /DA (/Helv 18 Tf {_BLUE}) /AP << /N 6 0 R >> >>"
    )


def freetext_with_acroform() -> bytes:
    # An AcroForm with no fields of its own is enough to give the page a form
    # environment, which is what sends it down the flatten branch.
    return _build(_freetext(), _value_appearance("N/A"), "<< /Fields [] >>")


def freetext_without_acroform() -> bytes:
    return _build(_freetext(), _value_appearance("N/A"), None)


# name -> (builder, whether the page has a blue value to find)
SHAPES: Dict[str, Tuple[Callable[[], bytes], bool]] = {
    "plain": (plain, False),
    "widget_needs_appearance": (widget_needs_appearance, True),
    "widget_with_appearance": (widget_with_appearance, True),
    "freetext_with_acroform": (freetext_with_acroform, True),
    "freetext_without_acroform": (freetext_without_acroform, True),
}


def blue_pixels_in_value_rect(image) -> int:
    """Count strongly blue pixels inside VALUE_RECT of a rendered PIL image."""
    rgb = image.convert("RGB")
    sx = rgb.width / PAGE_WIDTH
    sy = rgb.height / PAGE_HEIGHT
    x0, y0, x1, y1 = VALUE_RECT
    # PDF y grows upward; image y grows downward.
    box = (
        int(x0 * sx),
        int((PAGE_HEIGHT - y1) * sy),
        int(x1 * sx),
        int((PAGE_HEIGHT - y0) * sy),
    )
    region = rgb.crop(box)
    data = region.tobytes()
    return sum(
        1
        for i in range(0, len(data), 3)
        if data[i + 2] > 180 and data[i] < 90 and data[i + 1] < 90
    )


def dark_pixels(image) -> int:
    """Count near-black pixels anywhere on the page (the static label)."""
    gray = image.convert("L")
    return sum(1 for v in gray.tobytes() if v < 80)
