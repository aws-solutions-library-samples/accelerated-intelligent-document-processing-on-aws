#!/usr/bin/env python3
"""Synthetic Bank Statement generator with EXACT ground truth.

Each transaction Description embeds a unique 'SEQnnnnn' tag for exact completeness
measurement. Deterministic given params (no RNG) so regeneration is byte-stable.

build(rows, cols, lists, desc_len, ocr_noise, out) -> writes <out>.pdf + <out>.truth.json
"""

import json

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

MERCHANTS = [
    "AnyCompany Store",
    "Example Mart",
    "Sample Cafe",
    "Test Fuel Co",
    "Demo Pharmacy",
    "Placeholder Books",
    "Acme Utilities",
    "Widget Depot",
]
WIDE_COLS = [
    "Date",
    "Description",
    "Amount",
    "Balance",
    "Category",
    "Ref",
    "Type",
    "Location",
]

# Fixed top-level fields (exact GT for scalar accuracy)
FIELDS = {
    "Account Number": "000123456789",
    "Statement Period": "01/01/2024 - 12/31/2024",
}


def _noise(s, seq, level):
    """Deterministic OCR-like corruption: drop/merge chars based on seq, no RNG."""
    if level <= 0:
        return s
    out = []
    for i, ch in enumerate(s):
        # drop roughly `level` fraction of spaces/digits deterministically
        if ch == " " and ((seq * 7 + i) % max(1, int(1 / level))) == 0:
            continue  # merge tokens (removes a space)
        out.append(ch)
    return "".join(out)


# --- value_noise: render cells in the formats a typed schema has to convert ----
#
# The default renderer already writes `MM/DD/2024` and a bare `-1234.56`, which a
# model returns essentially verbatim — so per-row VALUE handling is invisible to
# the benchmark. `value_noise=True` renders each row's cells the way real
# statements actually do (currency symbols, accounting negatives, thousands
# separators in both conventions, named months, boolean-ish words) while the truth
# records the TYPED target. The row's SEQ tag is untouched, so completeness
# scoring is unaffected and `cell_accuracy` measures value fidelity alone.
#
# Every case here is UNAMBIGUOUS on purpose. Deliberately excluded: a numeric
# date whose day is <= 12 (e.g. `03/04/1985`), because no reader can tell M/D
# from D/M and the correct pipeline behaviour is to REFUSE, not to guess — a
# benchmark that scored a guess as correct would reward exactly the wrong thing.
_AMOUNT_STYLES = 6


def _amount_rendered(value, seq):
    """One of six real-world renderings of ``value``, chosen deterministically."""
    style = seq % _AMOUNT_STYLES
    mag = abs(value)
    neg = value < 0
    grouped = f"{mag:,.2f}"  # 1,234.56
    if style == 0:
        return f"${grouped}" if not neg else f"-${grouped}"
    if style == 1:  # accounting negative
        return f"({grouped})" if neg else grouped
    if style == 2:  # European convention: '.' groups, ',' decimals
        euro = grouped.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
        return f"EUR {euro}" if not neg else f"EUR -{euro}"
    if style == 3:
        return f"{grouped} USD" if not neg else f"-{grouped} USD"
    if style == 4:  # apostrophe grouping (CH)
        return ("-" if neg else "") + f"{mag:,.2f}".replace(",", "'")
    return ("-" if neg else "") + grouped


_MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]  # fmt: skip


def _date_pair(seq):
    """(rendered, iso) for a date that is UNAMBIGUOUS in every rendering.

    Day is forced into 13..28 so the day/month order can never be misread — the
    point is to measure conversion, not to reward a coin-flip.
    """
    month = (seq % 12) + 1
    day = 13 + (seq % 16)  # 13..28
    iso = f"2024-{month:02d}-{day:02d}"
    style = seq % 3
    if style == 0:
        return f"{month:02d}/{day:02d}/2024", iso
    if style == 1:
        return f"{day} {_MONTHS[month - 1]} 2024", iso
    return f"{_MONTHS[month - 1]} {day}, 2024", iso


def _row(seq, cols, desc_len, noise, value_noise=False):
    """Returns ``(cells, typed)`` — typed is the schema-typed truth for the row."""
    date = f"{(seq % 12) + 1:02d}/{(seq % 28) + 1:02d}/2024"
    merch = MERCHANTS[seq % len(MERCHANTS)]
    if desc_len == "long":
        desc = (
            f"SEQ{seq:05d} Purchase at {merch} - authorization code "
            f"{seq * 7 % 999999:06d} - recurring monthly charge reference "
            f"invoice {seq} terminal {seq % 50}"
        )
    else:
        desc = f"SEQ{seq:05d} {merch} #{seq}"
    amount_value = round(((seq * 13.7) % 5000) * (-1 if seq % 3 else 1), 2)
    amount = f"{'-' if seq % 3 else ''}{(seq * 13.7) % 5000:.2f}"
    typed = None
    if value_noise:
        date, iso = _date_pair(seq)
        amount = _amount_rendered(amount_value, seq)
        typed = {"Date": iso, "Amount": amount_value}
    if noise:
        desc = _noise(desc, seq, noise)
    if cols == 3:
        return [date, desc, amount], typed
    return [
        date,
        desc,
        amount,
        f"{(seq * 31.1) % 99999:.2f}",
        ["Food", "Fuel", "Retail", "Bills"][seq % 4],
        f"R{seq:06d}",
        ["DEBIT", "CREDIT"][seq % 2],
        f"City{seq % 40}, ST",
    ], typed


def build(
    rows,
    cols=3,
    lists=1,
    desc_len="short",
    ocr_noise=0.0,
    value_noise=False,
    documents=1,
    paginate=False,
    out="doc.pdf",
):
    """``documents=N`` emits N back-to-back COMPLETE statements in one file.

    This is the over-MERGE direction of boundary detection, and it is the case a
    naive over-split fix regresses: a prompt biased toward ``continue`` collapses
    N statements into one section. #653 measured the unfixed prompt at 1/10 on
    this shape, so testing only the over-split direction would hide a fix that
    made this worse.

    Each copy carries its own opening identity block (distinct account number)
    and per-copy pagination footers, so the correct answer is unambiguous:
    exactly ``documents`` sections. The truth records that as
    ``expected_sections``.
    """
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        out,
        pagesize=letter,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
        leftMargin=0.4 * inch,
        rightMargin=0.4 * inch,
    )
    story = []
    header = WIDE_COLS[:cols] if cols > 3 else ["Date", "Description", "Amount"]
    fontsize = 7 if cols > 3 else 8
    colwidths = [0.9 * inch, 4.8 * inch, 1.0 * inch] if cols == 3 else None
    seq = 0
    seq_ids, per_list = [], {}
    rows_typed = {}
    rpl = rows // lists
    for di in range(documents):
        if di:
            story.append(PageBreak())
        # Opening identity block. A DISTINCT account number per copy is what makes
        # "this is a new document" recoverable from the page's own evidence, which
        # is what the boundary rules are told to look for.
        acct = (
            FIELDS["Account Number"]
            if di == 0
            else f"{int(FIELDS['Account Number']) + di:012d}"
        )
        story += [
            Paragraph("<b>AnyBank Monthly Statement</b>", styles["Title"]),
            Paragraph(f"Account Number: {acct}", styles["Normal"]),
            Paragraph(
                f"Statement Period: {FIELDS['Statement Period']}", styles["Normal"]
            ),
            Paragraph(
                "Account Holder: Jane Q Public, 100 Main Street, Anytown, CA 90210",
                styles["Normal"],
            ),
            Spacer(1, 0.2 * inch),
        ]
        # `rows` is PER DOCUMENT, so each copy gets a full table rather than the
        # first copy consuming the whole budget. `seq` keeps counting across
        # copies so every SEQ tag stays globally unique for completeness scoring.
        doc_start_seq = seq
        for li in range(lists):
            n = rpl if li < lists - 1 else (rows - (seq - doc_start_seq))
            if lists > 1:
                story.append(
                    Paragraph(f"<b>Transaction Ledger {li + 1}</b>", styles["Heading2"])
                )
            data = [header]
            ids = []
            for _ in range(n):
                cells, typed = _row(seq, cols, desc_len, ocr_noise, value_noise)
                data.append(cells)
                if typed:
                    rows_typed[f"SEQ{seq:05d}"] = typed
                ids.append(f"SEQ{seq:05d}")
                seq_ids.append(f"SEQ{seq:05d}")
                seq += 1
            t = Table(data, repeatRows=1, colWidths=colwidths)
            t.setStyle(
                TableStyle(
                    [
                        ("FONTSIZE", (0, 0), (-1, -1), fontsize),
                        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                        ("TOPPADDING", (0, 0), (-1, -1), 1),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                    ]
                )
            )
            story.append(t)
            story.append(Spacer(1, 0.15 * inch))
            per_list[f"list{li + 1}"] = ids
    if paginate:
        # Real statements paginate, and "Page 2 of 3" is the single most decisive
        # boundary signal a page can carry — the classification boundary rules
        # check it FIRST (#653). A corpus document without it tests those rules
        # with their primary evidence removed, which is a legitimate hard case but
        # not the common one. Two passes, because the total page count is not known
        # until the document has been laid out once.
        from copy import copy as _shallow

        counter = {"total": 0}

        def _count(canvas, _doc):
            counter["total"] = max(counter["total"], canvas.getPageNumber())

        doc.build([_shallow(f) for f in story], onFirstPage=_count, onLaterPages=_count)
        total = counter["total"] or 1

        def _footer(canvas, doc_):
            canvas.saveState()
            canvas.setFont("Helvetica", 8)
            canvas.drawRightString(
                7.9 * inch, 0.32 * inch, f"Page {canvas.getPageNumber()} of {total}"
            )
            canvas.restoreState()

        doc = SimpleDocTemplate(
            out,
            pagesize=letter,
            topMargin=0.5 * inch,
            bottomMargin=0.5 * inch,
            leftMargin=0.4 * inch,
            rightMargin=0.4 * inch,
        )
        doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    else:
        doc.build(story)
    truth = {
        "gen": "bank_statement",
        "rows": rows,
        "cols": cols,
        "lists": lists,
        "desc_len": desc_len,
        "ocr_noise": ocr_noise,
        "value_noise": value_noise,
        "paginate": paginate,
        "fields": dict(FIELDS),
        "seq_ids": seq_ids,
        "per_list": per_list if lists > 1 else None,
        "list_key": "Transactions",
        "documents": documents,
        # The number of sections a correct boundary detection must produce.
        "expected_sections": documents,
        # Only present under value_noise. Omitted otherwise so every existing
        # truth file is byte-identical and the committed baseline stays comparable.
        **({"rows_typed": rows_typed} if rows_typed else {}),
    }
    json.dump(truth, open(out + ".truth.json", "w"))
    try:
        import pypdfium2 as p

        pages = len(p.PdfDocument(out))
    except Exception:
        pages = None
    return {"out": out, "rows": rows, "pages": pages, "documents": documents}
