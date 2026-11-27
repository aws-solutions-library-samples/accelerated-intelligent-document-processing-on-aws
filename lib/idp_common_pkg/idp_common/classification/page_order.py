# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Reading printed page numbers off OCR text, to classify pages in printed order.

``classification.document_shape.pageOrder: printed_page_numbers`` asks the
classification step to look at the pages in the order their printed page numbers
say, rather than the order they were scanned in. This module is the deterministic
half of that: it reads each page's OCR text (no model call), finds the page number
printed in the page's header or footer, and decides whether the numbers are good
enough to sort by. :class:`~idp_common.classification.service.ClassificationService`
does the rest.

The rule is deliberately all-or-nothing. A document is reordered only when **every**
page carries exactly one readable number and the numbers form one gap-free run with
no repeats (and, where pages print a total, every page prints the same total). Any
other outcome keeps the scan order and says why, because a partial or guessed order
is worse than the scan order: it would move pages the reader can no longer find
where they put them, on evidence that did not hold for the whole document.

**Where a number is looked for.** Only in the first and last
:data:`ZONE_LINES` non-empty lines of a page's text, which is where running headers
and footers land in the text an OCR engine produces. The text is first cut at the
first note the OCR step appended after it (the signature summary or the
handwriting legend, each recognised by its exact header line), and the
``(HANDWRITING)`` marker is removed from each line before it is matched, so neither
takes the place of a real header or footer line. A number in the body of the
page (a quantity, a reference to "page 4 of the instructions", a table cell) is not
in that zone on a page of ordinary length and is never read. A short page whose
whole text fits in the zone is read whole, which is the one place a body number can
be mistaken for a page number; the sequence check is what catches that, because a
stray number rarely completes a gap-free run with the other pages.

**What counts as a page number**, in two tiers. A page's reading comes from the
stronger tier that found anything on it; within a tier, two different numbers make
the page ambiguous.

* *Explicit* forms: ``Page 3 of 5`` or ``Pg. 3/5`` anywhere on a zone line;
  ``Page 3``, ``p. 3``, ``3 of 5`` or ``- 3 -`` (any dash) as a whole line; and
  ``Page 3`` ending a zone line that holds other text (``Form 1040 (2023)  Page 2``,
  ``Rev. 01/2026 • Page 2``) when it reads as a *label*: ``Page`` or ``PAGE`` with
  a capital, and set apart from the text before it — that text ends in something
  other than a letter. ``continued on page 3`` and ``see Page 3`` are part of a
  sentence and are not read. A label after a separator still is, so
  ``Continued on reverse - Page 2`` reads as 2; the sequence check is the backstop
  for that shape.
* *Bare* forms, used only when a page has no explicit one: a whole line that is a
  number alone (``3``) or a fraction (``3/5``). A bare numeral is also how an
  amount or a box value on a form looks once OCR has put it on a line of its own,
  so a document that relies on any bare reading is reordered only when its numbers
  run from 1; an explicit-only document may start anywhere (an excerpt numbered
  5 to 7).

Numbers of more than three digits are not read, which keeps a year (``2024``)
from passing as a page number. A ``N of M`` whose ``N`` exceeds ``M`` is not a page
number either.

What it does not read: roman numerals, letters (``A-1``), numbers that exist only in
the page image (the OCR text is the only input), and per-document numbering inside
a packet. A packet whose documents each start at page 1 repeats numbers, so it keeps
scan order with a ``duplicate`` reason — which is the correct answer, since one
sequence cannot order several documents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from idp_common.config.models import PAGE_ORDER_VALUES

#: The two values of ``classification.document_shape.pageOrder``, declared beside
#: the setting in ``idp_common.config.models``.
PAGE_ORDER_SCAN, PAGE_ORDER_PRINTED = PAGE_ORDER_VALUES

#: Non-empty lines read at the top and at the bottom of each page.
ZONE_LINES = 3

#: The ``ProcessingIssue`` code recorded when the setting is on and the scan order
#: was kept.
PAGE_ORDER_ISSUE_CODE = "classification_page_order_not_applied"

#: Markdown and table decoration stripped from both ends of a line before matching.
_DECORATION = " \t#*_>|`~"

#: A page number: at most three digits, which keeps a year (``2024``) out.
_NUM = r"(\d{1,3})(?!\d)"
_DASH = r"[-–—]"

# Explicit, anywhere on a zone line.
_PAGE_N_OF_M = re.compile(
    r"\b(?:page|pg\.?)\s*:?\s*" + _NUM + r"\s*(?:of|/)\s*" + _NUM + r"(?!\d)",
    re.IGNORECASE,
)
# Explicit, ending a zone line that also holds other text — when it is a label
# rather than part of a sentence; see _is_label.
_PAGE_N_AT_END = re.compile(r"\b(?:Page|PAGE|Pg\.?|PG\.?)\s*:?\s*" + _NUM + r"$")
# Explicit, as a whole line: (pattern, has_total).
_WHOLE_LINE_EXPLICIT: Tuple[Tuple["re.Pattern[str]", bool], ...] = (
    (re.compile(r"(?:page|pg\.?|p\.)\s*:?\s*" + _NUM, re.IGNORECASE), False),
    (re.compile(_NUM + r"\s+of\s+" + _NUM, re.IGNORECASE), True),
    (re.compile(_DASH + r"\s*" + _NUM + r"\s*" + _DASH), False),
)
# Bare, as a whole line.
_WHOLE_LINE_BARE: Tuple[Tuple["re.Pattern[str]", bool], ...] = (
    (re.compile(_NUM), False),
    (re.compile(_NUM + r"\s*/\s*" + _NUM), True),
)


@dataclass(frozen=True)
class PageNumberReading:
    """What one page's header and footer say its number is.

    ``number`` is set only when the page reads as exactly one number.
    ``candidates`` holds every distinct number of the tier that decided, so an
    ambiguous page can say what it read.
    """

    number: Optional[int] = None
    total: Optional[int] = None
    candidates: Tuple[int, ...] = ()
    has_text: bool = True
    bare: bool = False
    conflicting_totals: bool = False

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1 or self.conflicting_totals


@dataclass
class PageOrderPlan:
    """The decision for one document.

    ``order`` is the page IDs in printed order when the numbers can be trusted,
    and ``None`` when the scan order is kept, in which case ``reason_code`` and
    ``reason`` say why and ``problem_page_ids`` names the pages concerned.
    """

    order: Optional[List[str]]
    numbers: Dict[str, int] = field(default_factory=dict)
    reason_code: Optional[str] = None
    reason: Optional[str] = None
    problem_page_ids: List[str] = field(default_factory=list)


# Notes the OCR step appends after a page's text, each after a blank line and
# starting with its own header line: the signature-detection summary and the
# ocr.mark_handwriting legend. They are not printed on the page, so they must not
# take up the footer zone. This is a copy of idp_common.ocr.service.OCR_NOTE_HEADERS
# (classification does not import ocr); a test keeps the two equal.
#
# Only these exact headers count. A looser pattern would also match text a page
# prints itself (a "--- OCR Report ---" line, say), and cutting the text there
# drops the real footer and can move a body line such as "p. 4" into the footer
# zone, where it would be read as the page number. A page that prints one of these
# exact headers after a blank line has the same exposure.
_OCR_NOTE_HEADERS = (
    "--- OCR handwriting markers ---",
    "--- OCR signature detections ---",
)
_OCR_NOTE_HEADER_RE = re.compile(
    r"\n[ \t]*\n(?:"
    + "|".join(re.escape(header) for header in _OCR_NOTE_HEADERS)
    + r")[ \t]*$",
    re.M,
)


def _without_ocr_notes(text: str) -> str:
    """The page text up to the first appended OCR note."""
    match = _OCR_NOTE_HEADER_RE.search(text)
    return text[: match.start()] if match else text


# The in-line marker ocr.mark_handwriting puts after handwritten words. A page
# number Textract typed as handwriting ("Page 2 (HANDWRITING)") is still the page
# number, so the marker is removed before a line is matched. Kept as a literal
# here rather than imported from idp_common.ocr, so the two packages stay
# independent.
_HANDWRITING_MARKER_RE = re.compile(r"[ \t]*\(HANDWRITING\)")


def _zone_lines(text: str) -> List[str]:
    lines = [
        _HANDWRITING_MARKER_RE.sub("", line).strip(_DECORATION)
        for line in _without_ocr_notes(text).splitlines()
    ]
    lines = [line for line in lines if line]
    if len(lines) <= 2 * ZONE_LINES:
        return lines
    return lines[:ZONE_LINES] + lines[-ZONE_LINES:]


def _is_label(line: str, start: int) -> bool:
    """Whether a ``Page N`` ending ``line`` at ``start`` stands apart as a label.

    It does when nothing precedes it, or what precedes it ends in a character
    that is not a letter (``)``, ``•``, ``|``, a digit, a dash). After a word it
    is part of a sentence, and in a sentence it names some other page.
    """
    before = line[:start].rstrip()
    return not before or not before[-1].isalpha()


def _accept(number: int, total: Optional[int]) -> bool:
    if number < 1:
        return False
    return total is None or number <= total


def read_printed_page_number(text: Optional[str]) -> PageNumberReading:
    """Read the printed page number from one page's OCR text."""
    if text is None or not text.strip():
        return PageNumberReading(has_text=False)

    explicit: List[Tuple[int, Optional[int]]] = []
    bare: List[Tuple[int, Optional[int]]] = []
    for line in _zone_lines(text):
        for match in _PAGE_N_OF_M.finditer(line):
            explicit.append((int(match.group(1)), int(match.group(2))))
        end = _PAGE_N_AT_END.search(line)
        if end and _is_label(line, end.start()):
            explicit.append((int(end.group(1)), None))
        for pattern, has_total in _WHOLE_LINE_EXPLICIT:
            whole = pattern.fullmatch(line)
            if whole:
                explicit.append(
                    (int(whole.group(1)), int(whole.group(2)) if has_total else None)
                )
        for pattern, has_total in _WHOLE_LINE_BARE:
            whole = pattern.fullmatch(line)
            if whole:
                bare.append(
                    (int(whole.group(1)), int(whole.group(2)) if has_total else None)
                )

    for found, is_bare in ((explicit, False), (bare, True)):
        usable = [(n, t) for n, t in found if _accept(n, t)]
        if not usable:
            continue
        numbers = tuple(sorted({n for n, _ in usable}))
        totals = {t for _, t in usable if t is not None}
        if len(numbers) == 1 and len(totals) > 1:
            # "Page 2 of 4" and "Page 2 of 5" on one page: the number agrees, the
            # document it numbers does not.
            return PageNumberReading(
                candidates=numbers, bare=is_bare, conflicting_totals=True
            )
        if len(numbers) == 1:
            return PageNumberReading(
                number=numbers[0],
                total=next(iter(totals)) if len(totals) == 1 else None,
                candidates=numbers,
                bare=is_bare,
            )
        return PageNumberReading(candidates=numbers, bare=is_bare)
    return PageNumberReading()


def _listed(page_ids: Sequence[str], limit: int = 10) -> str:
    shown = ", ".join(page_ids[:limit])
    if len(page_ids) > limit:
        shown += f" and {len(page_ids) - limit} more"
    return shown


def plan_printed_page_order(
    page_ids: Sequence[str], texts: Mapping[str, Optional[str]]
) -> PageOrderPlan:
    """Decide the printed order of ``page_ids`` (given in scan order) from their text.

    Returns a plan whose ``order`` is ``None`` unless every page has exactly one
    number, every page that prints a total prints the same one, and the numbers
    are distinct and gap-free.
    """
    readings = {pid: read_printed_page_number(texts.get(pid)) for pid in page_ids}

    no_text = [pid for pid in page_ids if not readings[pid].has_text]
    if no_text:
        return PageOrderPlan(
            order=None,
            reason_code="no_text",
            reason=(
                f"page(s) {_listed(no_text)} have no OCR text to read a page "
                f"number from"
            ),
            problem_page_ids=no_text,
        )
    ambiguous = [pid for pid in page_ids if readings[pid].ambiguous]
    if ambiguous:
        first = ambiguous[0]
        read_as = " and ".join(str(n) for n in readings[first].candidates)
        if readings[first].conflicting_totals:
            read_as += " with two different page totals"
        return PageOrderPlan(
            order=None,
            reason_code="ambiguous",
            reason=(
                f"page(s) {_listed(ambiguous)} show more than one candidate page "
                f"number (page {first} reads as {read_as})"
            ),
            problem_page_ids=ambiguous,
        )
    missing = [pid for pid in page_ids if readings[pid].number is None]
    if missing:
        return PageOrderPlan(
            order=None,
            reason_code="no_number",
            reason=(
                f"no printed page number was found in the header or footer of "
                f"page(s) {_listed(missing)}"
            ),
            problem_page_ids=missing,
        )

    numbers: Dict[str, int] = {}
    for pid in page_ids:
        number = readings[pid].number
        assert number is not None  # every page was checked above
        numbers[pid] = number
    totals = sorted({r.total for r in readings.values() if r.total is not None})
    if len(totals) > 1:
        with_total = [pid for pid in page_ids if readings[pid].total is not None]
        return PageOrderPlan(
            order=None,
            numbers=numbers,
            reason_code="inconsistent_totals",
            reason=(
                "the pages print different page totals "
                f"({', '.join(str(t) for t in totals)}), so they are not one "
                "numbered sequence"
            ),
            problem_page_ids=with_total,
        )

    beyond = [pid for pid in page_ids if totals and numbers[pid] > totals[0]]
    if beyond:
        return PageOrderPlan(
            order=None,
            numbers=numbers,
            reason_code="inconsistent_totals",
            reason=(
                f"page(s) {_listed(beyond)} are numbered beyond the printed page "
                f"total of {totals[0]}"
            ),
            problem_page_ids=beyond,
        )

    by_number: Dict[int, List[str]] = {}
    for pid in page_ids:
        by_number.setdefault(numbers[pid], []).append(pid)
    repeated = [pids for pids in by_number.values() if len(pids) > 1]
    if repeated:
        first = repeated[0]
        problem = [pid for pids in repeated for pid in pids]
        return PageOrderPlan(
            order=None,
            numbers=numbers,
            reason_code="duplicate",
            reason=(
                f"page number {numbers[first[0]]} is printed on more than one page "
                f"({_listed(first)}), so the numbers do not identify one sequence"
            ),
            problem_page_ids=problem,
        )

    ordered = sorted(page_ids, key=lambda pid: numbers[pid])
    low, high = numbers[ordered[0]], numbers[ordered[-1]]
    if high - low + 1 != len(page_ids):
        absent = sorted(set(range(low, high + 1)) - set(numbers.values()))
        return PageOrderPlan(
            order=None,
            numbers=numbers,
            reason_code="gap",
            reason=(
                f"the printed page numbers run from {low} to {high} with "
                f"{_listed([str(n) for n in absent])} missing"
            ),
            problem_page_ids=list(page_ids),
        )
    bare = [pid for pid in page_ids if readings[pid].bare]
    if bare and low != 1:
        return PageOrderPlan(
            order=None,
            numbers=numbers,
            reason_code="unanchored",
            reason=(
                f"the numbers on page(s) {_listed(bare)} are bare numerals, and a "
                f"run of them that starts at {low} rather than 1 is as likely to be "
                "amounts or codes as page numbers"
            ),
            problem_page_ids=bare,
        )
    return PageOrderPlan(order=ordered, numbers=numbers)
