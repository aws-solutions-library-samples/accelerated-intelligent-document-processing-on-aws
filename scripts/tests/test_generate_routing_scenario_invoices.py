# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The review routing scenario invoices print what their truth file says.

``scripts/generate_routing_scenario_invoices.py`` writes the invoices under
``samples/review-routing/`` and ``scenarios.truth.json`` beside them. Each invoice
carries problems that were chosen in advance: a vendor name in a short form, a net
amount that is not the gross total minus the discount, payment terms that are
missing, a PO-backed invoice with no purchase order. The showcase preset's routing
rules and its live check are written against those values, so a page that prints
something else makes a scenario land in the wrong queue with nothing here to say
why.

The values are therefore read back from the rendered page, for a fresh run and for
the committed files, and compared with a table written out in this file rather than
with the generator's own data.

``reportlab`` renders and ``pypdfium2`` reads back. Both are declared in
``lib/idp_common_pkg``'s ``test`` extra and imported plainly, so a missing one is
an error and not a skip.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pypdfium2
import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
GENERATOR = REPO / "scripts" / "generate_routing_scenario_invoices.py"
COMMITTED = REPO / "samples" / "review-routing"
TRUTH_NAME = "scenarios.truth.json"

FIELDS = (
    "Vendor",
    "InvoiceNumber",
    "InvoiceType",
    "PaymentTerms",
    "GrossTotal",
    "Discount",
    "NetAmountDue",
)
AMOUNTS = ("GrossTotal", "Discount", "NetAmountDue")
PAGE_LABELS = dict(
    zip(
        FIELDS,
        (
            "Vendor",
            "Invoice Number",
            "Invoice Type",
            "Payment Terms",
            "Gross Total",
            "Discount",
            "Net Amount Due",
        ),
    )
)

# What each invoice prints, in the order of FIELDS. None: the page does not print
# the field at all.
SCENARIO_TABLE = {
    "routing-scenario-one-problem.pdf": (
        "AnyCo Office",
        "INV-1001",
        "Standard",
        "Net 30",
        "840.00",
        "40.00",
        "800.00",
    ),
    "routing-scenario-two-problems.pdf": (
        "Example Print Services",
        "INV-2002",
        "Standard",
        None,
        "1,200.00",
        "100.00",
        "1,250.00",
    ),
    "routing-scenario-three-problems.pdf": (
        "AnyCo Office",
        "INV-3003",
        "PO-backed",
        None,
        "2,000.00",
        "0.00",
        "2,000.00",
    ),
}


@pytest.fixture(scope="module")
def generator():
    spec = importlib.util.spec_from_file_location(GENERATOR.stem, GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generated(generator, tmp_path_factory) -> Path:
    out_dir = tmp_path_factory.mktemp("review-routing")
    generator.build(out_dir)
    return out_dir


@pytest.fixture(params=["generated", "committed"])
def folder(request, generated) -> Path:
    """A fresh run's output, then the files the repository ships."""
    return generated if request.param == "generated" else COMMITTED


def _pages(path: Path) -> list[str]:
    doc = pypdfium2.PdfDocument(str(path))
    try:
        return [doc[i].get_textpage().get_text_range() for i in range(len(doc))]
    finally:
        doc.close()


def test_two_runs_write_the_same_files(generator, tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    for out_dir in (first, second):
        out_dir.mkdir()
        generator.build(out_dir)

    assert (first / TRUTH_NAME).read_bytes() == (second / TRUTH_NAME).read_bytes()
    for name in SCENARIO_TABLE:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_a_run_writes_the_invoices_and_the_truth_file_and_nothing_else(generated):
    assert sorted(p.name for p in generated.iterdir()) == sorted(
        [*SCENARIO_TABLE, TRUTH_NAME]
    )


def test_the_repository_ships_the_invoices_and_the_truth_file():
    """Every PDF in the folder is published as part of the batch, so the set is
    exact: a stray document there would be shipped as a scenario."""
    assert sorted(p.name for p in COMMITTED.glob("*.pdf")) == sorted(SCENARIO_TABLE)
    assert (COMMITTED / TRUTH_NAME).is_file()


def test_the_shipped_truth_is_what_the_generator_writes(generated):
    """An edit to the generator without a new run leaves the two apart."""
    assert (COMMITTED / TRUTH_NAME).read_text(encoding="utf-8") == (
        generated / TRUTH_NAME
    ).read_text(encoding="utf-8")


def test_the_truth_matches_the_scenario_table(generator, folder):
    truth = json.loads((folder / TRUTH_NAME).read_text(encoding="utf-8"))

    assert truth["document_class"] == "Scenario-Invoice"
    assert [d["file"] for d in truth["documents"]] == list(SCENARIO_TABLE)
    assert [d["scenario"] for d in truth["documents"]] == ["one", "two", "three"]
    for document in truth["documents"]:
        fields = document["fields"]
        assert tuple(fields) == FIELDS, document["file"]
        assert document["page_count"] == 1, document["file"]
        # Amounts are numbers; the table holds them as the page prints them.
        for name in AMOUNTS:
            assert isinstance(fields[name], (int, float)), (document["file"], name)
        assert (
            tuple(generator.printed(name, fields[name]) for name in FIELDS)
            == SCENARIO_TABLE[document["file"]]
        ), document["file"]


@pytest.mark.parametrize("name", list(SCENARIO_TABLE))
def test_each_invoice_is_one_page_that_prints_its_values(folder, name):
    pages = _pages(folder / name)
    assert len(pages) == 1
    text = pages[0]

    for field, value in zip(FIELDS, SCENARIO_TABLE[name]):
        label = PAGE_LABELS[field]
        if value is None:
            # Neither a value nor an empty label: nothing to extract.
            assert label not in text, f"{name} prints {label!r}"
            continue
        # The value directly after its own label, and complete: "40.00" inside
        # "840.00" or after another label is not the discount.
        printed = re.search(rf"{re.escape(label)}:?\s+{re.escape(value)}(?!\S)", text)
        assert printed, f"{name} does not print {label} {value}:\n{text}"


@pytest.mark.parametrize("name", list(SCENARIO_TABLE))
def test_a_page_prints_one_spelling_of_its_vendor(folder, name):
    """The short name "AnyCo Office" is a problem only while the page offers no
    other name to extract. Its registered form starts with "AnyCompany"."""
    text = _pages(folder / name)[0]
    vendor = SCENARIO_TABLE[name][0]

    assert text.count(vendor) == 1
    assert "AnyCompany" not in text


def test_line_items_that_do_not_add_up_are_refused(generator, tmp_path, monkeypatch):
    """The net amount is wrong on purpose on one invoice. A wrong sum of the
    lines would be a second problem on a page that is meant to carry a known
    number of them."""
    broken = json.loads(json.dumps(generator.INVOICES[0]))
    broken["lines"][0][1] += 1
    monkeypatch.setattr(generator, "INVOICES", [broken])

    with pytest.raises(ValueError, match="add up to 885.00"):
        generator.build(tmp_path)
    assert not (tmp_path / TRUTH_NAME).exists()
