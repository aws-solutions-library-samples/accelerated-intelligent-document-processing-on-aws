# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Pipeline assembly merges the Map results over the document's sections.

A run plan that names sections sends only those through the Map. The assembly
step puts each result in the place of the section it is about and keeps the
others as the document had them. A Map over every section gives the list it
always gave, which the last class compares against a copy of the loop the handler
used before.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from idp_common.models import Document, Section, Status
from tests.unit.lambdas._assembly import (
    assembled,
    processresults_index,
    run_pipeline,
    section_result,
)
from tests.unit.lambdas._review_env import ALERT, BUCKET, KEY, config, result_uri

pytestmark = pytest.mark.unit


def _section(section_id, *, marker="base", alerts=0, missing=0):
    """A section; ``marker`` tells a base section from a Map result of it."""
    return Section(
        section_id=section_id,
        classification=f"Invoice-{marker}",
        page_ids=[section_id],
        extraction_result_uri=result_uri(section_id),
        confidence_threshold_alerts=[dict(ALERT) for _ in range(alerts)],
        missing_required_fields=[{"attribute_name": "Vendor"}] * missing,
    )


def _base(*sections):
    return Document(
        id=KEY,
        input_bucket="in",
        input_key=KEY,
        output_bucket=BUCKET,
        status=Status.EXTRACTING,
        num_pages=max(len(sections), 1),
        config_version="default",
        sections=list(sections),
    )


def _whole_document_result(sections, section_id):
    """A Map result that holds every section (the extraction skip returns that)."""
    doc = _base(*sections)
    return {"section_id": section_id, "document": doc.to_dict()}


def _run(base, results, *, store=None, hitl=False):
    response = run_pipeline(store, base, results, cfg=config(hitl=hitl))
    return response, assembled(response)


def _summary(document):
    return [(s["section_id"], s["classification"]) for s in document["sections"]]


def _named(*ids, marker="base"):
    return [SimpleNamespace(section_id=i, marker=marker) for i in ids]


class TestMergeSections:
    """``merge_sections(base, results)``, on its own."""

    def _merge(self, base, results):
        merged = processresults_index.merge_sections(base, results)
        return [(s.section_id, s.marker) for s in merged]

    def test_a_result_takes_the_place_of_its_section(self):
        merged = self._merge(_named("1", "2", "3"), _named("2", marker="map"))

        assert merged == [("1", "base"), ("2", "map"), ("3", "base")]

    def test_base_order_wins_over_result_order(self):
        merged = self._merge(_named("1", "2", "3"), _named("3", "1", marker="map"))

        assert merged == [("1", "map"), ("2", "base"), ("3", "map")]

    def test_a_result_the_base_lacks_follows_in_result_order(self):
        merged = self._merge(_named("1"), _named("9", "1", "7", marker="map"))

        assert merged == [("1", "map"), ("9", "map"), ("7", "map")]

    def test_no_base_is_the_result_list(self):
        assert self._merge([], _named("2", "1", marker="map")) == [
            ("2", "map"),
            ("1", "map"),
        ]

    def test_no_results_is_the_base(self):
        assert self._merge(_named("1", "2"), []) == [("1", "base"), ("2", "base")]


class TestAMapOverSomeSections:
    def test_the_others_are_kept_in_the_documents_order(self):
        base = _base(_section("1"), _section("2"), _section("3"))

        _, document = _run(base, [section_result(_section("2", marker="map"))])

        assert _summary(document) == [
            ("1", "Invoice-base"),
            ("2", "Invoice-map"),
            ("3", "Invoice-base"),
        ]

    def test_a_kept_section_is_the_one_the_document_had(self):
        base = _base(_section("1", alerts=2), _section("2"))

        _, document = _run(base, [section_result(_section("2", marker="map"))])

        assert document["sections"][0] == base.to_dict()["sections"][0]

    def test_the_counts_cover_kept_sections_too(self):
        base = _base(_section("1", alerts=2, missing=1), _section("2"), _section("3"))
        seen = []
        service = SimpleNamespace(
            get_document=lambda key: None,
            update_document=lambda d: seen.append(
                (d.confidence_alert_count, d.missing_required_field_count)
            ),
        )

        run_pipeline(
            None,
            base,
            [section_result(_section("2", marker="map", alerts=1, missing=2))],
            cfg=config(hitl=False),
            service=service,
        )

        # The first write is the status change, before the sections are merged.
        assert seen[-1] == (3, 3)

    def test_the_legacy_flow_asks_a_review_for_a_kept_section(self):
        """As it did when every section went through the Map."""
        base = _base(_section("1", alerts=1), _section("2"))

        response, document = _run(
            base, [section_result(_section("2", marker="map"))], hitl=True
        )

        assert response["hitl_triggered"] is True
        assert [m["record_number"] for m in document["hitl_metadata"]] == [1]

    def test_the_legacy_flow_asks_no_review_for_sections_without_alerts(self):
        base = _base(_section("1"), _section("2"))

        response, document = _run(
            base, [section_result(_section("2", marker="map"))], hitl=True
        )

        assert response["hitl_triggered"] is False
        assert document.get("hitl_metadata", []) == []


def _rebuild_as_before(results):
    """The section list as the handler built it before run plans.

    A copy of the loop the handler had: every Map result contributes the section
    it names (the first one of its document when it names none), in result
    order, and the list starts empty.
    """
    sections = []
    for result in results:
        section_document = Document.from_dict(result.get("document", {}))
        if section_document.sections:
            wanted = result.get("section_id")
            sections.append(
                next(
                    (s for s in section_document.sections if s.section_id == wanted),
                    section_document.sections[0],
                )
            )
    return Document(sections=sections).to_dict()["sections"]


_THREE = [_section(i, marker="map", alerts=int(i) - 1) for i in ("1", "2", "3")]

FULL_MAPS = {
    "one-section-per-result": [section_result(s) for s in _THREE],
    "results-that-name-no-section": [
        section_result(s, name_section=False) for s in _THREE
    ],
    "results-that-hold-the-whole-document": [
        _whole_document_result(_THREE, s.section_id) for s in _THREE
    ],
    "one-section": [section_result(_THREE[0])],
}


class TestAMapOverEverySection:
    @pytest.mark.parametrize("case", sorted(FULL_MAPS))
    def test_the_sections_are_the_ones_the_earlier_loop_built(self, case):
        results = FULL_MAPS[case]
        expected = _rebuild_as_before(results)
        base = _base(*[_section(s["section_id"]) for s in expected])

        _, document = _run(base, results)

        assert document["sections"] == expected
        assert all(s["classification"] == "Invoice-map" for s in document["sections"])

    def test_a_document_that_arrives_with_no_sections_gets_the_results(self):
        """The base of a run from the start can be a document not yet split."""
        results = FULL_MAPS["one-section-per-result"]

        _, document = _run(_base(), results)

        assert document["sections"] == _rebuild_as_before(results)
