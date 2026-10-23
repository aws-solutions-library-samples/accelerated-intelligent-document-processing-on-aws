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
    Flowable,
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


# --- Per-copy pagination ------------------------------------------------------
#
# ``documents=N`` emits N complete statements in one file and the truth records
# ``expected_sections: N``. A footer numbering pages across the WHOLE FILE
# contradicts that: a 3-copy file reads "Page 1 of 3 / Page 2 of 3 / Page 3 of 3",
# which is the single most decisive "this is one 3-page document" signal a page can
# carry, and the classification boundary rules check it FIRST. A correct classifier
# then returns 1 section against a truth file claiming 3 — a right answer scored as
# wrong, with the model's own reason citing the footer. So pagination is scoped to
# the copy: each statement is numbered "Page k of <its own pages>", which both
# agrees with ``expected_sections`` and makes the combination a usable over-merge
# fixture instead of a trap.
#
# The page map has to come from the real layout rather than from an assumption
# about how many pages a copy takes, so it is recorded by a marker flowable during
# the pass that ``paginate`` already runs to learn the total page count.


def _account_number(index):
    """The account number document copy ``index`` carries — distinct per copy.

    A distinct number is what makes "this is a new document" recoverable from the
    page's own evidence, which is what the boundary rules are told to look for.
    """
    if index == 0:
        return FIELDS["Account Number"]
    return f"{int(FIELDS['Account Number']) + index:012d}"


class _CopyStart(Flowable):
    """Zero-size marker recording which page a document copy starts on.

    It keeps a reference to the caller's dict rather than its own state, so it
    still reports into that dict through the shallow copy of every flowable that
    the measuring pass makes.
    """

    def __init__(self, index, starts):
        Flowable.__init__(self)
        self._index = index
        self._starts = starts

    def wrap(self, *_args):
        return (0, 0)

    def draw(self):
        self._starts[self._index] = self.canv.getPageNumber()


def _page_spans(starts, total, documents):
    """``[(first_page, pages)]`` per copy, derived from the measured layout.

    Raises rather than guessing. Every copy but the first is preceded by a
    ``PageBreak``, so the first pages are strictly increasing and a map that is not
    says the markers did not all draw — in which case the footers would silently
    revert to file-wide numbering, which is the defect this exists to prevent.
    """
    firsts = [starts.get(index) for index in range(documents)]
    if None in firsts or sorted(firsts) != firsts or len(set(firsts)) != documents:
        raise RuntimeError(
            f"pagination page map is incomplete: {documents} document copies but "
            f"recorded start pages {firsts}. Per-copy footers cannot be numbered "
            "from this, and numbering them across the file would contradict "
            "expected_sections."
        )
    spans = []
    for index in range(documents):
        after = firsts[index + 1] if index + 1 < documents else total + 1
        spans.append((firsts[index], max(1, after - firsts[index])))
    return spans


def _locate(page, spans):
    """``(copy index, page within that copy, pages in that copy)`` for ``page``."""
    for index, (first, pages) in enumerate(spans):
        if first <= page < first + pages:
            return index, page - first + 1, pages
    index = len(spans) - 1
    first, pages = spans[index]
    return index, page - first + 1, pages


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
    # Per-CELL truth for EVERY row, not just the value-noise variant.
    #
    # `rows_typed` is what feeds `score_cells` -> `cell_accuracy`, the only metric
    # that can see a per-row VALUE problem. It used to be emitted only under
    # `value_noise`, which left every other document — including all seven in
    # `core_synth`, the grid the config-guidance paper reports — with no per-cell
    # instrument at all. That is not a theoretical gap: on `longdesc_100` simple
    # extraction returns all 100 rows with `Amount: null`, and because
    # `completeness_recall` counts SEQ tags in the Description and
    # `scalar_accuracy` only looks at document-level fields, the run scored
    # recall 1.000 / accuracy 1.000 with an entire column empty. Two studies drew
    # conclusions from that blind spot (the first enforcement A/B, and the WS-05
    # forcing A/B's "no measurable effect").
    #
    # The rendered date is unambiguous MDY here (`02/14/2024`), so the ISO form is
    # derivable without the `_date_pair` machinery that `value_noise` needs.
    typed = {
        "Date": f"2024-{(seq % 12) + 1:02d}-{(seq % 28) + 1:02d}",
        "Amount": amount_value,
    }
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


def _wrap_description(cells, desc_len, styles, fontsize):
    """Make a long Description cell WRAP instead of overprinting its neighbour.

    A plain string in a reportlab ``Table`` cell does not wrap — it is drawn on one
    line and simply runs past the column edge. At ``desc_len="long"`` the
    description is ~120 characters in a 4.8-inch column, so it overprinted the
    Amount column to its right and Textract read the collision rather than the
    two values: OCR of ``longdesc_100`` contained **zero** amount-shaped lines,
    and one row came back as ``"...recurring monthly charge00ference invoice 0"``
    — the amount ``0.00`` stamped over the word ``reference``.

    That made every Amount on that document physically unreadable, so it was not a
    test of extraction at all. It went unnoticed because the only metric that
    could have seen it (``cell_accuracy``) was not emitted for this document —
    see the ``rows_typed`` note in ``_row``.

    ⚠️ This changes the rendering (and therefore the page count) of any
    ``desc_len="long"`` document — today only ``longdesc_100``. Results for that
    document are NOT comparable across this fix. Short descriptions are left as
    plain strings so every other document renders byte-for-byte as before.
    """
    if desc_len != "long":
        return cells
    style = styles["BodyText"].clone("cellwrap")
    style.fontSize = fontsize
    style.leading = fontsize + 1
    style.spaceBefore = style.spaceAfter = 0
    out = list(cells)
    out[1] = Paragraph(str(out[1]), style)
    return out


def build(
    rows,
    cols=3,
    lists=1,
    desc_len="short",
    ocr_noise=0.0,
    value_noise=False,
    documents=1,
    paginate=False,
    repeat_header=False,
    out="doc.pdf",
):
    """``documents=N`` emits N back-to-back COMPLETE statements in one file.

    ``repeat_header=True`` reprints a running identity header — bank name, account
    number, statement period, but NOT the account holder's name and address — at the
    top of every page after the first (the #750 shape, and the boundary rules'
    known failure mode). It composes with ``paginate`` (header at the top, "Page N of
    M" at the bottom). The header carries the account number of the copy whose page
    it is on, so with ``documents > 1`` it reprints each statement's own identity
    rather than the first statement's.

    This is the over-MERGE direction of boundary detection, and it is the case a
    naive over-split fix regresses: a prompt biased toward ``continue`` collapses
    N statements into one section. #653 measured the unfixed prompt at 1/10 on
    this shape, so testing only the over-split direction would hide a fix that
    made this worse.

    Each copy carries its own opening identity block (distinct account number)
    and, under ``paginate``, its own pagination footers restarting at "Page 1 of
    <its own pages>" — see the per-copy pagination note above for why file-wide
    numbering made this combination unusable. So the correct answer is unambiguous:
    exactly ``documents`` sections. The truth records that as ``expected_sections``.
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
    # Both page-drawn signals — the pagination footer and the running identity
    # header — have to say which COPY a page belongs to once there is more than one,
    # or they assert the opposite of `expected_sections`. Only then is the marker
    # needed, which keeps every single-document fixture byte-identical.
    needs_page_map = documents > 1 and (paginate or repeat_header)
    copy_starts = {}
    for di in range(documents):
        if di:
            story.append(PageBreak())
        if needs_page_map:
            story.append(_CopyStart(di, copy_starts))
        # Opening identity block.
        acct = _account_number(di)
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
                data.append(_wrap_description(cells, desc_len, styles, fontsize))
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

    top_margin = 0.85 * inch if repeat_header else 0.5 * inch
    total, spans = 1, []

    if paginate or needs_page_map:
        # Real statements paginate, and "Page 2 of 3" is the single most decisive
        # boundary signal a page can carry — the classification boundary rules
        # check it FIRST (#653). A corpus document without it tests those rules
        # with their primary evidence removed, which is a legitimate hard case but
        # not the common one. Two passes, because neither the total page count nor
        # which pages belong to which copy is known until the story has been laid
        # out once.
        #
        # The measuring pass uses the SAME margins as the real one. It used to lay
        # out at the default top margin while `repeat_header` raised it for the real
        # build, so the two passes could disagree on the page count and the footer
        # would then quote a total the file does not have.
        from copy import copy as _shallow

        counter = {"total": 0}

        def _count(canvas, _doc):
            counter["total"] = max(counter["total"], canvas.getPageNumber())

        SimpleDocTemplate(
            out,
            pagesize=letter,
            topMargin=top_margin,
            bottomMargin=0.5 * inch,
            leftMargin=0.4 * inch,
            rightMargin=0.4 * inch,
        ).build([_shallow(f) for f in story], onFirstPage=_count, onLaterPages=_count)
        total = counter["total"] or 1
        spans = (
            _page_spans(copy_starts, total, documents)
            if needs_page_map
            else [(1, total)]
        )

    def _running_header(canvas, _doc):
        # The OTHER common real-world shape (#726 counter-case, #750): many statements
        # print a running identity header — bank name, account number, statement
        # period — at the top of EVERY page, with no addressee name or address. A
        # boundary rule that keys on "carries an identity block => start" over-splits
        # exactly this shape; drawn on the canvas so it does not disturb the table.
        index = _locate(canvas.getPageNumber(), spans)[0] if spans else 0
        canvas.saveState()
        canvas.setFont("Helvetica-Bold", 9)
        canvas.drawString(0.4 * inch, 10.6 * inch, "AnyBank Monthly Statement")
        canvas.setFont("Helvetica", 8)
        canvas.drawString(
            0.4 * inch,
            10.45 * inch,
            f"Account Number: {_account_number(index)}    "
            "Statement Period: 01/01/2024 - 12/31/2024",
        )
        canvas.restoreState()

    if paginate:

        def _footer(canvas, _doc):
            # Numbered within the COPY, not across the file.
            _, page, pages = _locate(canvas.getPageNumber(), spans)
            canvas.saveState()
            canvas.setFont("Helvetica", 8)
            canvas.drawRightString(7.9 * inch, 0.32 * inch, f"Page {page} of {pages}")
            canvas.restoreState()

        def _later(canvas, doc_):
            _footer(canvas, doc_)
            if repeat_header:
                _running_header(canvas, doc_)

        doc = SimpleDocTemplate(
            out,
            pagesize=letter,
            topMargin=top_margin,
            bottomMargin=0.5 * inch,
            leftMargin=0.4 * inch,
            rightMargin=0.4 * inch,
        )
        doc.build(story, onFirstPage=_footer, onLaterPages=_later)
    elif repeat_header:
        doc = SimpleDocTemplate(
            out,
            pagesize=letter,
            topMargin=top_margin,
            bottomMargin=0.5 * inch,
            leftMargin=0.4 * inch,
            rightMargin=0.4 * inch,
        )
        doc.build(story, onLaterPages=_running_header)
    else:
        doc.build(story)
    truth = {
        "gen": "bank_statement",
        "repeat_header": bool(repeat_header),
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
