# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Generate the synthetic invoices of the review routing scenarios.

The ``review-routing-showcase`` preset routes a document to a review queue for a
reason a reviewer can act on. Showing that needs documents whose problems are
known in advance, and no other sample has controlled problems. This script
renders single-page invoices that carry one, two and three of them:

    routing-scenario-one-problem.pdf      the vendor name is a short form, not a
                                          registered name
    routing-scenario-two-problems.pdf     the net amount due is not the gross
                                          total minus the discount, and no
                                          payment terms are printed
    routing-scenario-three-problems.pdf   the invoice type is PO-backed and the
                                          document holds no purchase order, the
                                          vendor name is a short form, and no
                                          payment terms are printed

Every problem is a property of what the page prints. A value that is missing is
missing from the page altogether (the label is not printed either), so an
extraction has nothing to read in its place.

``scenarios.truth.json`` records, per invoice, the value of each field of the
preset's ``Scenario-Invoice`` class as the page prints it. ``null`` means the
page does not print the field. Amounts are numbers there and are printed with a
thousands separator and two decimals (``1250.0`` is printed ``1,250.00``).

Deterministic: no random values, and the PDFs are built in reportlab's invariant
mode (a fixed creation date and document id), so a second run with the same
reportlab version writes the same bytes.

Usage::

    python scripts/generate_routing_scenario_invoices.py \
        --out-dir samples/review-routing

writes the PDFs and ``scenarios.truth.json`` into that directory.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# The class of the showcase preset these invoices are extracted as. Its field
# names are the keys of every ``fields`` mapping below.
DOCUMENT_CLASS = "Scenario-Invoice"
TRUTH_NAME = "scenarios.truth.json"

# Field name -> the label the page prints for it, in the order the fields are
# recorded in the truth file.
LABELS: Dict[str, str] = {
    "Vendor": "Vendor",
    "InvoiceNumber": "Invoice Number",
    "InvoiceType": "Invoice Type",
    "PaymentTerms": "Payment Terms",
    "GrossTotal": "Gross Total",
    "Discount": "Discount",
    "NetAmountDue": "Net Amount Due",
}
AMOUNT_FIELDS = ("GrossTotal", "Discount", "NetAmountDue")

# Fictitious names only (AWS docs convention: AnyCompany / Example).
BILL_TO = ["Example Corp", "Accounts Payable", "123 Any Street", "Any Town, USA"]

# One entry per invoice. ``fields`` is what the page prints and what the truth
# file records. ``lines`` are (description, quantity, unit price) and add up to
# the gross total. The vendor address never repeats the vendor's name, so the
# page prints one spelling of it.
INVOICES: List[Dict[str, Any]] = [
    {
        "scenario": "one",
        "file": "routing-scenario-one-problem.pdf",
        "vendor_address": ["200 Any Street", "Any Town, USA", "206-555-0100"],
        "invoice_date": "03/02/2026",
        "fields": {
            "Vendor": "AnyCo Office",
            "InvoiceNumber": "INV-1001",
            "InvoiceType": "Standard",
            "PaymentTerms": "Net 30",
            "GrossTotal": 840.00,
            "Discount": 40.00,
            "NetAmountDue": 800.00,
        },
        "lines": [
            ("Copy paper, case of ten reams", 12, 45.00),
            ("Toner cartridge, black", 4, 75.00),
        ],
    },
    {
        "scenario": "two",
        "file": "routing-scenario-two-problems.pdf",
        "vendor_address": ["45 Example Avenue", "Any Town, USA", "206-555-0125"],
        "invoice_date": "03/09/2026",
        "fields": {
            "Vendor": "Example Print Services",
            "InvoiceNumber": "INV-2002",
            "InvoiceType": "Standard",
            "PaymentTerms": None,
            "GrossTotal": 1200.00,
            "Discount": 100.00,
            # Not the gross total minus the discount: that is this invoice's
            # first problem.
            "NetAmountDue": 1250.00,
        },
        "lines": [
            ("Brochure printing, 500 copies", 1, 800.00),
            ("Poster printing, large format", 20, 20.00),
        ],
    },
    {
        "scenario": "three",
        "file": "routing-scenario-three-problems.pdf",
        "vendor_address": ["200 Any Street", "Any Town, USA", "206-555-0100"],
        "invoice_date": "03/16/2026",
        "fields": {
            "Vendor": "AnyCo Office",
            "InvoiceNumber": "INV-3003",
            "InvoiceType": "PO-backed",
            "PaymentTerms": None,
            "GrossTotal": 2000.00,
            "Discount": 0.00,
            "NetAmountDue": 2000.00,
        },
        "lines": [
            ("Office chair, adjustable", 8, 150.00),
            ("Desk lamp", 20, 40.00),
        ],
    },
]

PAGE_W = letter[0]
MARGIN = 0.9 * inch
# The width a table may take: the page between the margins, less the frame's own
# padding on each side.
FRAME_W = PAGE_W - 2 * MARGIN - 12

_styles = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=_styles["Normal"], fontSize=10.5, leading=14)
TITLE = ParagraphStyle(
    "title", parent=BODY, fontSize=22, leading=26, fontName="Helvetica-Bold"
)
SMALL = ParagraphStyle(
    "small", parent=BODY, fontSize=8.5, leading=11, textColor=colors.HexColor("#333333")
)

_PLAIN = [
    ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
    ("FONTSIZE", (0, 0), (-1, -1), 10.5),
    ("TOPPADDING", (0, 0), (-1, -1), 2),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
]


def money(value: float) -> str:
    """An amount as the page prints it: ``1250.0`` -> ``1,250.00``."""
    return f"{value:,.2f}"


def printed(field: str, value: Any) -> Optional[str]:
    """The text the page prints for a field, or ``None`` when it prints nothing."""
    if value is None:
        return None
    return money(value) if field in AMOUNT_FIELDS else str(value)


def _check(invoice: Dict[str, Any]) -> None:
    """Refuse an entry whose line items do not add up to its gross total.

    The net amount due is not checked: one invoice prints a wrong one on purpose.
    A wrong sum of the lines would be a problem nobody chose.
    """
    if list(invoice["fields"]) != list(LABELS):
        raise ValueError(f"{invoice['file']}: fields must be {list(LABELS)}")
    total = round(sum(qty * price for _, qty, price in invoice["lines"]), 2)
    if total != invoice["fields"]["GrossTotal"]:
        raise ValueError(
            f"{invoice['file']}: the line items add up to {money(total)}, not to "
            f"the gross total {money(invoice['fields']['GrossTotal'])}"
        )


def _block(rows: List[Tuple[str, str]], widths: List[float]) -> Table:
    """Rows of ``label: value`` with bold labels.

    A row with an empty label continues the value of the row above it.
    """
    table = Table(
        [[f"{label}:" if label else "", value] for label, value in rows],
        colWidths=widths,
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(_PLAIN + [("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold")])
    )
    return table


def _flowables(invoice: Dict[str, Any]) -> List[Any]:
    fields = invoice["fields"]

    def row(field: str) -> List[Tuple[str, str]]:
        text = printed(field, fields[field])
        # A field with no value is left off the page, label included.
        return [] if text is None else [(LABELS[field], text)]

    vendor_rows = row("Vendor") + [("", line) for line in invoice["vendor_address"]]
    detail_rows = (
        row("InvoiceNumber")
        + [("Invoice Date", invoice["invoice_date"])]
        + row("InvoiceType")
        + row("PaymentTerms")
    )
    header = Table(
        [
            [
                _block(vendor_rows, [0.75 * inch, 2.45 * inch]),
                _block(detail_rows, [1.3 * inch, 1.6 * inch]),
            ]
        ],
        colWidths=[FRAME_W * 0.52, FRAME_W * 0.48],
        hAlign="LEFT",
    )
    header.setStyle(
        TableStyle(
            [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]
        )
    )

    bill_to = _block(
        [("Bill To", BILL_TO[0])] + [("", line) for line in BILL_TO[1:]],
        [0.75 * inch, 3.0 * inch],
    )

    items = Table(
        [["Description", "Quantity", "Unit Price", "Amount"]]
        + [
            [description, str(qty), money(price), money(qty * price)]
            for description, qty, price in invoice["lines"]
        ],
        colWidths=[FRAME_W * 0.49, FRAME_W * 0.15, FRAME_W * 0.18, FRAME_W * 0.18],
        hAlign="LEFT",
    )
    items.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10.5),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EFEFEF")),
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.black),
                ("LINEBELOW", (0, -1), (-1, -1), 0.6, colors.black),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )

    totals = Table(
        [[LABELS[field], money(fields[field])] for field in AMOUNT_FIELDS],
        colWidths=[1.6 * inch, 1.2 * inch],
        hAlign="RIGHT",
    )
    totals.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 10.5),
                ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
                ("LINEABOVE", (0, -1), (-1, -1), 0.6, colors.black),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )

    return [
        Paragraph("INVOICE", TITLE),
        Spacer(1, 16),
        header,
        Spacer(1, 18),
        bill_to,
        Spacer(1, 20),
        items,
        Spacer(1, 12),
        totals,
        Spacer(1, 28),
        Paragraph("All amounts are in US dollars.", SMALL),
    ]


def _render(invoice: Dict[str, Any], path: Path) -> int:
    """Write one invoice and return its page count."""
    doc = SimpleDocTemplate(
        str(path),
        pagesize=letter,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=MARGIN,
        bottomMargin=MARGIN,
        title=f"Invoice {invoice['fields']['InvoiceNumber']}",
        author=invoice["fields"]["Vendor"],
        # A fixed creation date and document id: the same input gives the same file.
        invariant=1,
    )
    doc.build(_flowables(invoice))
    return doc.page


def build(out_dir: Path) -> Dict[str, Any]:
    """Write the PDFs and the truth file into ``out_dir`` and return the truth."""
    documents = []
    for invoice in INVOICES:
        _check(invoice)
        pages = _render(invoice, out_dir / invoice["file"])
        if pages != 1:
            raise ValueError(f"{invoice['file']} has {pages} pages; it must have one")
        documents.append(
            {
                "scenario": invoice["scenario"],
                "file": invoice["file"],
                "page_count": pages,
                "fields": dict(invoice["fields"]),
            }
        )
    truth = {
        "description": (
            "What each review routing scenario invoice prints, by field of the "
            f"{DOCUMENT_CLASS} class. null: the page does not print the field. "
            "Amounts are printed with a thousands separator and two decimals."
        ),
        "document_class": DOCUMENT_CLASS,
        "documents": documents,
    }
    (out_dir / TRUTH_NAME).write_text(
        json.dumps(truth, indent=2) + "\n", encoding="utf-8"
    )
    return truth


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--out-dir",
        default="samples/review-routing",
        help="Directory to write the PDFs and scenarios.truth.json into",
    )
    args = ap.parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    truth = build(out_dir)
    for document in truth["documents"]:
        values = ", ".join(
            f"{LABELS[field]} {printed(field, value) or '(none printed)'}"
            for field, value in document["fields"].items()
        )
        print(f"Wrote {out_dir / document['file']}: {values}")
    print(f"Wrote {out_dir / TRUTH_NAME}")


if __name__ == "__main__":
    main()
