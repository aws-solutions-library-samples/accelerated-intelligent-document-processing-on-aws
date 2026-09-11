#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Generate a synthetic multi-instance pay-statement packet with exact ground truth.

The document holds THREE pay statements for the same employee in one file, and it
is laid out so that **no statement boundary falls on a page boundary**:

    page 1  |  statement 1 (header .. taxes)
    page 2  |  statement 1 (tail) | statement 2 (header ..)
    page 3  |  statement 2 (tail) | statement 3 (header ..)
    page 4  |  statement 3 (tail)

Two properties are deliberate, because they are what makes the packet a
reproducer rather than just a multi-record document:

1. **Mid-page boundaries.** A section is a set of whole pages, so no page-level
   split can separate these three records. Page-level classification can only
   emit one ``start``/``continue`` flag per page, and pages 2 and 3 are each
   simultaneously the end of one record and the start of the next.
2. **A misleading document-wide pagination footer** (``N/4``) plus an identical
   banner on every page. Those are the strongest "this is one continuous
   document" cues a classifier has, and they are wrong here.

Deterministic: no RNG, so regeneration is byte-stable apart from the PDF's own
creation timestamp.

Usage::

    python3 scripts/generate_multi_instance_paystub.py \
        --out samples/paystub_multi_instance

writes ``<out>.pdf`` and ``<out>.truth.json``.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# Fictitious names only (AWS docs convention: AnyCompany / Example / Sample).
PAYROLL_PROVIDER = "AnyCompany Payroll Services, L.P."
PROVIDER_ADDRESS = "1400 Sample Parkway, Example City, TX 77339-3802"
PROVIDER_PHONE = "866-555-0142"
PORTAL = "portal.example.com"
WORKSITE_EMPLOYER = "EXAMPLE CORP, INC. (3514100)"
WORKSITE_ADDRESS_1 = "742 SAMPLE ST # 62284"
WORKSITE_ADDRESS_2 = "EXAMPLE CITY, CA 94104"
WORKSITE_PHONE = "(888) 555-0199"
EMPLOYEE_NAME = "RIVERA, JORDAN A"
EMPLOYEE_ID = "E-4471029"

EXPORT_STAMP = "04/16/2026, 9:18 AM"
FOOTER_URL = f"https://{PORTAL}/EPayStatement/PayStatement.Report.aspx"

# The boilerplate notice that opens each statement. Repeating it is realistic for
# a payroll-portal export and it also gives every page near-identical framing
# text, which is part of what the reproducer exercises.
NOTICE_TITLE = "Did You Know That AnyCompany Premier is the Place to Go for your..."
NOTICE_BODY = (
    "1. Personal employment information 2. Current and past paycheck records, W2 and W4 "
    "forms 3. Health insurance information and credentials 4. 401(k) plan account (if you "
    "have one) 5. Flexible spending account (FSA) plan or HSA (if you have one) "
    "6. AnyCompany contact information and tutorials/overview videos 7. MarketPlace access"
)
NOTICE_CTA = (
    f"Just log in to {PORTAL} for easy and instant access to this important "
    "information and much more!"
)

PTO_DISCLAIMER = (
    "Your worksite employer provides the Paid Time Off/Vacation and/or Sick Information "
    "shown. &ldquo;Balance&rdquo; reflects hours available for use under the PTO, vacation, "
    "and/or sick time provided to AnyCompany by your worksite employer. See your supervisor "
    "with questions about your Balance. If your Paid Time Off/Vacation and/or Sick "
    "Information does not appear, this information will be provided by your worksite "
    "employer, as applicable."
)

# Three statements for the SAME employee, most recent first — the shape a portal
# "print my last three statements" export produces. CheckNumber is the exact-match
# key a completeness check should assert on.
STATEMENTS: List[Dict[str, Any]] = [
    {
        "CheckNumber": "77310468",
        "PayDate": "04/15/2026",
        "PayPeriodStart": "04/01/2026",
        "PayPeriodEnd": "04/15/2026",
        "GrossEarnings": "6,250.00",
        "TotalTaxes": "1,520.41",
        "TotalDeductions": "625.00",
        "NetPay": "4,104.59",
        "earnings": [
            (
                "Salary - Exempt",
                "04/01/2026",
                "04/15/2026",
                "",
                "6,250.00",
                "6,250.00",
                "28,124.99",
            ),
            ("Commission - Sup", "", "", "", "", "", "27,450.49"),
            ("Retro Pay - Reg", "", "", "", "", "", "1,041.67"),
            ("Fringe - Reg", "", "", "", "", "", "278.00"),
        ],
        "gross_ytd": "56,895.15",
        "pretax": [("401K Plan", "312.50", "2,622.52")],
        "pretax_total": ("312.50", "2,622.52"),
        "aftertax": [
            ("Fringe Out", "0.00", "278.00"),
            ("401K Roth", "312.50", "2,726.68"),
        ],
        "aftertax_total": ("312.50", "3,004.68"),
        "taxes": [
            ("Federal Taxes", "955.58", "10,159.77"),
            ("SocSec", "387.50", "3,527.50"),
            ("Medicare", "90.63", "824.98"),
            ("WA Worker Comp Tax", "0.00", "0.00"),
            ("WA PFML", "50.45", "459.24"),
            ("WA LTC", "36.25", "329.99"),
        ],
        "taxes_total": ("1,520.41", "15,301.48"),
        "ytd_taxable": [
            ("Federal", "54,272.63"),
            ("Social Security", "56,895.15"),
            ("Medicare", "56,895.15"),
        ],
    },
    {
        "CheckNumber": "77298351",
        "PayDate": "03/31/2026",
        "PayPeriodStart": "03/16/2026",
        "PayPeriodEnd": "03/31/2026",
        "GrossEarnings": "7,450.67",
        "TotalTaxes": "1,904.56",
        "TotalDeductions": "888.16",
        "NetPay": "4,657.95",
        "earnings": [
            (
                "Salary - Exempt",
                "03/16/2026",
                "03/31/2026",
                "",
                "6,250.00",
                "6,250.00",
                "21,874.99",
            ),
            (
                "Retro Pay - Reg",
                "03/16/2026",
                "03/31/2026",
                "",
                "",
                "1,041.67",
                "1,041.67",
            ),
            ("Fringe - Reg", "03/16/2026", "03/31/2026", "", "", "159.00", "278.00"),
            ("Commission - Sup", "", "", "", "", "", "27,450.49"),
        ],
        "gross_ytd": "50,645.15",
        "pretax": [("401K Plan", "364.58", "2,310.02")],
        "pretax_total": ("364.58", "2,310.02"),
        "aftertax": [
            ("Fringe Out", "159.00", "278.00"),
            ("401K Roth", "364.58", "2,414.18"),
        ],
        "aftertax_total": ("523.58", "2,692.18"),
        "taxes": [
            ("Federal Taxes", "1,231.24", "9,204.19"),
            ("SocSec", "461.94", "3,140.00"),
            ("Medicare", "108.03", "734.35"),
            ("WA Worker Comp Tax", "0.00", "0.00"),
            ("WA PFML", "60.14", "408.79"),
            ("WA LTC", "43.21", "293.74"),
        ],
        "taxes_total": ("1,904.56", "13,781.07"),
        "ytd_taxable": [
            ("Federal", "48,335.13"),
            ("Social Security", "50,645.15"),
            ("Medicare", "50,645.15"),
        ],
    },
    {
        "CheckNumber": "77284207",
        "PayDate": "03/13/2026",
        "PayPeriodStart": "03/01/2026",
        "PayPeriodEnd": "03/15/2026",
        "GrossEarnings": "27,450.49",
        "TotalTaxes": "8,217.89",
        "TotalDeductions": "2,745.04",
        "NetPay": "16,487.56",
        "earnings": [
            (
                "Commission - Sup",
                "03/01/2026",
                "03/15/2026",
                "",
                "",
                "27,450.49",
                "27,450.49",
            ),
            ("Salary - Exempt", "", "", "", "", "", "10,416.66"),
            ("Fringe - Reg", "", "", "", "", "", "119.00"),
        ],
        "gross_ytd": "37,986.15",
        "pretax": [("401K Plan", "1,372.52", "1,685.02")],
        "pretax_total": ("1,372.52", "1,685.02"),
        "aftertax": [
            ("Fringe Out", "0.00", "119.00"),
            ("401K Roth", "1,372.52", "1,789.18"),
        ],
        "aftertax_total": ("1,372.52", "1,908.18"),
        "taxes": [
            ("Federal Taxes", "5,737.15", "7,252.33"),
            ("SocSec", "1,701.93", "2,355.14"),
            ("Medicare", "398.03", "550.80"),
            ("WA Worker Comp Tax", "0.00", "0.00"),
            ("WA PFML", "221.57", "306.61"),
            ("WA LTC", "159.21", "220.32"),
        ],
        "taxes_total": ("8,217.89", "10,685.20"),
        "ytd_taxable": [
            ("Federal", "36,301.13"),
            ("Social Security", "37,986.15"),
            ("Medicare", "37,986.15"),
        ],
    },
]

PAGE_W, PAGE_H = letter
MARGIN = 0.55 * inch
FRAME_W = PAGE_W - 2 * MARGIN

_styles = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=_styles["Normal"], fontSize=6.6, leading=8.2)
BOLD = ParagraphStyle("bold", parent=BODY, fontName="Helvetica-Bold")
NOTICE_H = ParagraphStyle(
    "noticeH", parent=BODY, fontSize=7.4, fontName="Helvetica-Bold"
)
SECTION_H = ParagraphStyle(
    "sectionH", parent=BODY, fontSize=7.2, fontName="Helvetica-Bold"
)
TINY = ParagraphStyle(
    "tiny", parent=BODY, fontSize=5.6, leading=6.8, textColor=colors.HexColor("#333333")
)

_GRID = TableStyle(
    [
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 6.2),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("LINEBELOW", (0, 0), (-1, 0), 0.4, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EFEFEF")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]
)


def _kv_table(rows: List[List[str]], widths: List[float]) -> Table:
    t = Table(rows, colWidths=widths, hAlign="LEFT")
    t.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 6.4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return t


def _grid_table(header: List[str], rows: List[Any], widths: List[float]) -> Table:
    t = Table(
        [header] + [list(r) for r in rows],
        colWidths=widths,
        hAlign="LEFT",
        repeatRows=0,
    )
    t.setStyle(_GRID)
    return t


def _statement_flowables(st: Dict[str, Any]) -> List[Any]:
    """One statement's flowables. Nothing here forces a page break — the records
    are allowed to flow across page boundaries, which is the whole point."""
    out: List[Any] = []

    # --- opening notice (kept together so it reads as one block) -------------
    out.append(
        KeepTogether(
            [
                Paragraph(NOTICE_TITLE, NOTICE_H),
                Spacer(1, 2),
                Paragraph(NOTICE_BODY, BODY),
                Spacer(1, 3),
                Paragraph(NOTICE_CTA, BODY),
            ]
        )
    )
    out.append(Spacer(1, 7))

    # --- identity / pay-period block ----------------------------------------
    left = [
        ["Employee:", EMPLOYEE_NAME],
        ["Company:", WORKSITE_EMPLOYER],
        ["", WORKSITE_ADDRESS_1],
        ["", WORKSITE_ADDRESS_2],
        ["Phone:", WORKSITE_PHONE],
    ]
    mid = [
        ["Employee ID:", EMPLOYEE_ID],
        ["Pay Date:", st["PayDate"]],
        ["PayPeriod:", f"{st['PayPeriodStart']} To {st['PayPeriodEnd']}"],
        ["Pay Frequency:", "SemiMonthly"],
    ]
    right = [
        ["Check #:", st["CheckNumber"]],
        ["Pay Type:", "Salary"],
        ["Department:", "400"],
        ["Location:", "USA - WA"],
    ]
    ident = Table(
        [
            [
                _kv_table(left, [0.62 * inch, 1.9 * inch]),
                _kv_table(mid, [0.78 * inch, 1.25 * inch]),
                _kv_table(right, [0.62 * inch, 1.0 * inch]),
            ]
        ],
        colWidths=[2.6 * inch, 2.1 * inch, 1.7 * inch],
        hAlign="LEFT",
    )
    ident.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    out.append(ident)
    out.append(Spacer(1, 5))

    # --- summary strip -------------------------------------------------------
    summary = Table(
        [
            [
                f"Gross Earnings: {st['GrossEarnings']}",
                f"Total Taxes: {st['TotalTaxes']}",
                f"Total Deductions: {st['TotalDeductions']}",
                f"Net Pay: {st['NetPay']}",
            ]
        ],
        colWidths=[FRAME_W / 4.0] * 4,
        hAlign="LEFT",
    )
    summary.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.0),
                ("LINEABOVE", (0, 0), (-1, 0), 0.5, colors.black),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.black),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    out.append(summary)
    out.append(Spacer(1, 7))

    # --- earnings ------------------------------------------------------------
    out.append(Paragraph("Earnings", SECTION_H))
    out.append(Spacer(1, 2))
    e_rows = [list(r) for r in st["earnings"]]
    e_rows.append(["Gross", "", "", "", "", st["GrossEarnings"], st["gross_ytd"]])
    out.append(
        _grid_table(
            [
                "Description",
                "Start Date",
                "End Date",
                "Hrs/Units",
                "Rate",
                "Current",
                "YTD",
            ],
            e_rows,
            [
                1.75 * inch,
                0.78 * inch,
                0.78 * inch,
                0.68 * inch,
                0.78 * inch,
                0.85 * inch,
                0.85 * inch,
            ],
        )
    )
    out.append(Spacer(1, 7))

    # --- deductions & taxes, side by side -----------------------------------
    ded_rows: List[List[str]] = [["Pre-Tax", "", ""]]
    ded_rows += [[d, c, y] for d, c, y in st["pretax"]]
    ded_rows.append(["Total", st["pretax_total"][0], st["pretax_total"][1]])
    ded_rows.append(["After Tax", "", ""])
    ded_rows += [[d, c, y] for d, c, y in st["aftertax"]]
    ded_rows.append(["Total", st["aftertax_total"][0], st["aftertax_total"][1]])
    ded_tbl = _grid_table(
        ["Description", "Current", "YTD"],
        ded_rows,
        [1.45 * inch, 0.72 * inch, 0.78 * inch],
    )

    tax_rows = [[d, c, y] for d, c, y in st["taxes"]]
    tax_rows.append(["Total", st["taxes_total"][0], st["taxes_total"][1]])
    tax_tbl = _grid_table(
        ["Description", "Current", "YTD"],
        tax_rows,
        [1.35 * inch, 0.72 * inch, 0.78 * inch],
    )

    two_up = Table(
        [
            [
                Paragraph("Deductions &amp; Credits", SECTION_H),
                Paragraph("Taxes", SECTION_H),
            ],
            [ded_tbl, tax_tbl],
        ],
        colWidths=[FRAME_W * 0.52, FRAME_W * 0.48],
        hAlign="LEFT",
    )
    two_up.setStyle(
        TableStyle(
            [("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, 0), 3)]
        )
    )
    out.append(two_up)
    out.append(Spacer(1, 7))

    # --- withholding elections + direct deposit -----------------------------
    elections = _kv_table(
        [
            ["Filing Status:", "Single or Married filing separately"],
            ["Two Jobs:", "No"],
            ["Claim Dependents:", "$ 0.00"],
            ["Deductions:", "$ 0.00"],
            ["Extra Withholding:", "$ 0.00"],
            ["Exempt:", "No"],
        ],
        [1.05 * inch, 2.0 * inch],
    )
    deposit = _grid_table(
        ["Account", "Number", "Amount"],
        [["Checking - AnyCompany Bank", "xxxxxx4471", st["NetPay"]]],
        [1.5 * inch, 0.85 * inch, 0.8 * inch],
    )
    two_up2 = Table(
        [
            [
                Paragraph("Direct Deposit Information", SECTION_H),
                Paragraph("Federal Tax Withholding Elections", SECTION_H),
            ],
            [deposit, elections],
        ],
        colWidths=[FRAME_W * 0.52, FRAME_W * 0.48],
        hAlign="LEFT",
    )
    two_up2.setStyle(
        TableStyle(
            [("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, 0), 3)]
        )
    )
    out.append(two_up2)
    out.append(Spacer(1, 7))

    # --- benefit elections + cost-centre allocation --------------------------
    # These two blocks exist to make one statement taller than one page. Without
    # them each statement happens to fit a page exactly, every boundary lands on
    # a page break, and the packet stops being a reproducer.
    benefits = _grid_table(
        ["Benefit", "Coverage", "Employee", "Employer"],
        [
            ["Medical PPO 1500", "Employee + Spouse", "218.44", "742.10"],
            ["Dental Plus", "Family", "38.90", "62.15"],
            ["Vision Basic", "Family", "9.12", "11.40"],
            ["Basic Life", "1x Salary", "0.00", "18.75"],
            ["Supplemental Life", "2x Salary", "14.60", "0.00"],
            ["Short Term Disability", "Employee", "11.25", "0.00"],
            ["Long Term Disability", "Employee", "0.00", "24.80"],
        ],
        [1.5 * inch, 1.05 * inch, 0.7 * inch, 0.72 * inch],
    )
    allocation = _grid_table(
        ["Cost Center", "Project", "Pct", "Amount"],
        [
            ["400 - Engineering", "PRJ-1042", "40.0%", "2,500.00"],
            ["400 - Engineering", "PRJ-1188", "25.0%", "1,562.50"],
            ["410 - Platform", "PRJ-2071", "15.0%", "937.50"],
            ["410 - Platform", "PRJ-2099", "10.0%", "625.00"],
            ["420 - Shared Svcs", "OVERHEAD", "6.0%", "375.00"],
            ["430 - Enablement", "TRAINING", "4.0%", "250.00"],
        ],
        [1.28 * inch, 0.82 * inch, 0.5 * inch, 0.78 * inch],
    )
    two_up_b = Table(
        [
            [
                Paragraph("Benefit Elections", SECTION_H),
                Paragraph("Earnings Allocation by Cost Center", SECTION_H),
            ],
            [benefits, allocation],
        ],
        colWidths=[FRAME_W * 0.52, FRAME_W * 0.48],
        hAlign="LEFT",
    )
    two_up_b.setStyle(
        TableStyle(
            [("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, 0), 3)]
        )
    )
    out.append(two_up_b)
    out.append(Spacer(1, 7))

    out.append(Paragraph("Messages", SECTION_H))
    out.append(Spacer(1, 2))
    out.append(
        Paragraph(
            "Open enrollment for the next plan year begins 05/01/2026. Review your elections "
            "in the portal; changes made after 05/31/2026 require a qualifying life event. "
            "Your 401(k) contribution rate is unchanged this period. Address and direct "
            "deposit changes take effect on the pay date following the change.",
            BODY,
        )
    )
    out.append(Spacer(1, 7))

    # --- PTO + YTD taxable ---------------------------------------------------
    pto = _grid_table(
        ["Plan Level", "Available", "Used", "Balance"],
        [["Sick and Safe Time Annual", "112.000", "0.000", "112.000 Hours"]],
        [1.5 * inch, 0.62 * inch, 0.5 * inch, 0.78 * inch],
    )
    ytd = _grid_table(
        ["Description", "Amount"],
        [[d, a] for d, a in st["ytd_taxable"]],
        [1.35 * inch, 1.0 * inch],
    )
    two_up3 = Table(
        [
            [
                Paragraph("Paid Time Off/Vacation and/or Sick Information", SECTION_H),
                Paragraph("AnyCompany YTD Taxable Amount", SECTION_H),
            ],
            [pto, ytd],
            [Paragraph(PTO_DISCLAIMER, TINY), ""],
        ],
        colWidths=[FRAME_W * 0.52, FRAME_W * 0.48],
        hAlign="LEFT",
    )
    two_up3.setStyle(
        TableStyle(
            [("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, 0), 3)]
        )
    )
    out.append(two_up3)
    out.append(Spacer(1, 6))

    # --- provider footer line (end-of-statement marker) ---------------------
    provider = Table(
        [[PAYROLL_PROVIDER, PROVIDER_ADDRESS, PROVIDER_PHONE]],
        colWidths=[FRAME_W * 0.3, FRAME_W * 0.48, FRAME_W * 0.22],
        hAlign="LEFT",
    )
    provider.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 6.0),
                ("LINEABOVE", (0, 0), (-1, 0), 0.4, colors.black),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )
    out.append(provider)
    out.append(Spacer(1, 12))
    return out


def _decorate(canvas, doc) -> None:
    """Per-page banner + document-wide ``N/4`` footer.

    These are the misleading cues: identical framing on every page and a
    pagination counter that describes the FILE, not the statement.
    """
    canvas.saveState()
    canvas.setFont("Helvetica", 6.2)
    canvas.drawString(MARGIN, PAGE_H - MARGIN + 12, EXPORT_STAMP)
    canvas.setFont("Helvetica-Bold", 6.8)
    canvas.drawCentredString(PAGE_W / 2.0, PAGE_H - MARGIN + 12, "PayStatement")
    canvas.setFont("Helvetica", 5.8)
    canvas.drawString(MARGIN, MARGIN - 14, FOOTER_URL)
    canvas.drawRightString(
        PAGE_W - MARGIN, MARGIN - 14, f"{canvas.getPageNumber()}/{doc.total_pages}"
    )
    canvas.restoreState()


class _Doc(SimpleDocTemplate):
    """SimpleDocTemplate that knows its own final page count.

    The footer prints ``N/<total>``, so the total must be known while drawing.
    Build twice: the first pass counts pages, the second prints the real total.
    """

    total_pages = 0


def build(out_stem: Path) -> Dict[str, Any]:
    def _story() -> List[Any]:
        # Rebuilt per pass: reportlab mutates flowables during layout, so the
        # same objects cannot be laid out twice.
        story: List[Any] = []
        for st in STATEMENTS:
            story.extend(_statement_flowables(st))
        return story

    def _make() -> _Doc:
        return _Doc(
            str(out_stem.with_suffix(".pdf")),
            pagesize=letter,
            leftMargin=MARGIN,
            rightMargin=MARGIN,
            topMargin=MARGIN + 4,
            bottomMargin=MARGIN + 4,
            title="Pay Statement Export",
            author=PAYROLL_PROVIDER,
        )

    # Pass 1: count pages (footer total is still 0, output discarded).
    counting = _make()
    counting.build(_story(), onFirstPage=_decorate, onLaterPages=_decorate)
    total = counting.page

    # Pass 2: real render with the correct total.
    final = _make()
    final.total_pages = total
    final.build(_story(), onFirstPage=_decorate, onLaterPages=_decorate)

    truth = {
        "document_type": "Pay-Statement",
        "instance_axis": "instances",
        "expected_instance_count": len(STATEMENTS),
        "page_count": total,
        "why_this_is_a_reproducer": (
            "Three same-class records whose boundaries fall mid-page, under a "
            "document-wide N/{total} footer and an identical per-page banner. No "
            "page-level section split can separate them.".format(total=total)
        ),
        "instances": [
            {
                "CheckNumber": st["CheckNumber"],
                "PayDate": st["PayDate"],
                "PayPeriodStart": st["PayPeriodStart"],
                "PayPeriodEnd": st["PayPeriodEnd"],
                "GrossEarnings": st["GrossEarnings"],
                "TotalTaxes": st["TotalTaxes"],
                "TotalDeductions": st["TotalDeductions"],
                "NetPay": st["NetPay"],
            }
            for st in STATEMENTS
        ],
    }
    out_stem.with_suffix(".truth.json").write_text(json.dumps(truth, indent=2) + "\n")
    return truth


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out",
        default="samples/paystub_multi_instance",
        help="Output path stem; writes <stem>.pdf and <stem>.truth.json",
    )
    args = ap.parse_args()
    stem = Path(args.out)
    stem.parent.mkdir(parents=True, exist_ok=True)
    truth = build(stem)
    print(f"Wrote {stem.with_suffix('.pdf')} ({truth['page_count']} pages)")
    print(f"Wrote {stem.with_suffix('.truth.json')}")
    for i, inst in enumerate(truth["instances"], 1):
        print(
            f"  instance {i}: Check #{inst['CheckNumber']}  Pay Date {inst['PayDate']}"
        )


if __name__ == "__main__":
    main()
