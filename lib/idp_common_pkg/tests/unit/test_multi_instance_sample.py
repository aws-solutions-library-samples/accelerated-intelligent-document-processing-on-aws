# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guard the properties that make ``samples/paystub_multi_instance.pdf`` a valid
reproducer for GitHub #715 / #753.

The asset only reproduces the bug because the three pay statements' boundaries
fall **mid-page**. An earlier draft of the generator laid out one record per
page, which silently destroys the whole point: page-level section splitting can
then separate the records, so nothing is lost and the reproducer proves nothing —
while still looking like a 4-page, 3-record document.

That failure mode is invisible in review, so it is asserted here instead. If
``scripts/generate_multi_instance_paystub.py`` changes record heights, this test
fails until the boundaries are mid-page again.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
PDF = REPO_ROOT / "samples" / "paystub_multi_instance.pdf"
TRUTH = REPO_ROOT / "samples" / "paystub_multi_instance.truth.json"

# The line that ENDS a statement (the provider footer of a record) and the line
# that STARTS the next one. Both on one page == a mid-page boundary.
RECORD_END_MARKER = "AnyCompany Payroll Services"
RECORD_START_MARKER = "Check #:"


def _page_texts() -> list[str]:
    pypdfium2 = pytest.importorskip(
        "pypdfium2", reason="PDF text extraction needs pypdfium2"
    )
    if not PDF.exists():  # pragma: no cover - asset is committed
        pytest.skip(f"sample not present: {PDF}")
    doc = pypdfium2.PdfDocument(str(PDF))
    try:
        return [doc[i].get_textpage().get_text_range() for i in range(len(doc))]
    finally:
        doc.close()


def test_truth_file_declares_three_instances_with_distinct_check_numbers():
    truth = json.loads(TRUTH.read_text())
    assert truth["expected_instance_count"] == 3
    assert truth["instance_axis"] == "instances"
    check_numbers = [i["CheckNumber"] for i in truth["instances"]]
    # The completeness keys: an extraction that loses records loses these.
    assert check_numbers == ["77310468", "77298351", "77284207"]
    assert len(set(check_numbers)) == 3


def test_sample_is_four_pages():
    assert len(_page_texts()) == 4


def test_record_boundaries_fall_mid_page_on_pages_two_and_three():
    """THE property. Each of pages 2 and 3 must carry the END of one statement
    and the START of the next, so no page-level split can separate the records.
    """
    texts = _page_texts()
    for page_no in (2, 3):
        text = texts[page_no - 1]
        assert RECORD_END_MARKER in text, (
            f"page {page_no} no longer contains the end of a statement "
            f"({RECORD_END_MARKER!r}) — the record boundary is no longer mid-page, "
            f"so the sample no longer reproduces #715/#753"
        )
        assert RECORD_START_MARKER in text, (
            f"page {page_no} no longer contains the start of a statement "
            f"({RECORD_START_MARKER!r}) — the record boundary is no longer mid-page, "
            f"so the sample no longer reproduces #715/#753"
        )


def test_every_check_number_appears_exactly_once_in_the_document():
    """Three distinct records, each present — so a lost record is detectable by
    its absent check number, and a duplicated one cannot inflate the count."""
    truth = json.loads(TRUTH.read_text())
    all_text = "\n".join(_page_texts())
    for record in truth["instances"]:
        assert all_text.count(record["CheckNumber"]) == 1, (
            f"check number {record['CheckNumber']} does not appear exactly once"
        )


def test_the_misleading_pagination_footer_and_banner_are_still_there():
    """A document-wide ``N/4`` footer plus an identical per-page banner is what
    makes the naive answers wrong — "count the pages" gives 4, "count the
    repeated headers" gives 4, and the right answer is 3. If these cues
    disappear, the sample stops testing the hard case."""
    texts = _page_texts()
    for page_no, text in enumerate(texts, start=1):
        assert f"{page_no}/4" in text, f"page {page_no} lost the N/4 footer"
        assert "PayStatement" in text, f"page {page_no} lost the per-page banner"
    # Three record-end markers for four pages: the count of repeated framing is
    # NOT the count of documents, which is the trap.
    assert sum(RECORD_END_MARKER in t for t in texts) == 3
