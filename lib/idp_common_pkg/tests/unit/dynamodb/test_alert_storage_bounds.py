# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The inline confidence-alert list must not be able to breach the item ceiling.

Alerts are stored inline in the tracking item, and EVERY section's alerts live in
the same item's `Sections` list — so bounding them here or nowhere. Unbounded, the
write fails and the document is left with no result at all, which is how two
production documents were lost (#814). Deduping fixed the duplication half; the
half it cannot touch is per-row alerts, which carry a row index, are legitimately
distinct and are exempt from the collapse (#813). A wide table with many
sub-threshold cells reaches the same ceiling with no duplication: 512 rows x 8
columns is ~4,096 alerts, ~450 KB at the ~110 bytes a serialized alert occupies
(#821).

Nothing recoverable is lost — this list is a derived copy of what
`explainability_info` holds in full on S3, and the Web UI's count already prefers
that source. What these tests pin is that the bound exists, that it keeps the
WORST findings, that the true count travels with a truncated list, and that an
ordinary document is completely unaffected.
"""

from typing import Any
from unittest.mock import Mock

import pytest

from idp_common.dynamodb.service import (
    _MAX_STORED_ALERTS_PER_DOCUMENT,
    _MAX_STORED_ALERTS_PER_SECTION,
    DocumentDynamoDBService,
    allocate_alert_budget,
    serialize_confidence_threshold_alerts,
    set_section_alerts,
)
from idp_common.models import Document, Section, Status

pytestmark = pytest.mark.unit


def _alert(name: str, confidence: float | None = 0.5) -> dict[str, Any]:
    alert: dict[str, Any] = {"attribute_name": name, "confidence_threshold": 0.8}
    if confidence is not None:
        alert["confidence"] = confidence
    return alert


def _section(section_id: str, alerts: list[dict[str, Any]]) -> Section:
    section = Section(
        section_id=section_id, classification="BankStatement", page_ids=["1"]
    )
    section.confidence_threshold_alerts = alerts
    return section


class TestTheSectionCap:
    def test_an_ordinary_section_is_stored_whole(self):
        """The common case must be byte-identical to before the cap existed."""
        section = _section(
            "1", [_alert(f"Transactions[{i}].Amount") for i in range(12)]
        )
        section_data: dict[str, Any] = {}

        set_section_alerts(section_data, section)

        assert len(section_data["ConfidenceThresholdAlerts"]) == 12
        assert "ConfidenceThresholdAlertsTotal" not in section_data

    def test_a_wide_table_is_capped_and_says_so(self):
        """The #821 shape: thousands of genuinely distinct per-row alerts."""
        section = _section(
            "1", [_alert(f"Transactions[{i}].Amount") for i in range(4096)]
        )
        section_data: dict[str, Any] = {}

        set_section_alerts(section_data, section)

        assert (
            len(section_data["ConfidenceThresholdAlerts"])
            == _MAX_STORED_ALERTS_PER_SECTION
        )
        assert section_data["ConfidenceThresholdAlertsTotal"] == 4096

    def test_the_kept_alerts_are_the_worst_ones(self):
        """A reviewer opening a truncated list should see the lowest scores, not
        whichever rows happened to come first."""
        alerts = [_alert(f"row[{i}]", confidence=0.9) for i in range(200)]
        alerts[7]["confidence"] = 0.10
        alerts[150]["confidence"] = 0.05
        section = _section("1", alerts)
        section_data: dict[str, Any] = {}

        set_section_alerts(section_data, section)

        kept = section_data["ConfidenceThresholdAlerts"]
        names = [entry["attributeName"] for entry in kept]
        assert "row[7]" in names
        assert "row[150]" in names
        # Original relative order is preserved, so identical runs write an
        # identical item instead of churning it.
        assert names.index("row[7]") < names.index("row[150]")

    def test_a_scoreless_alert_never_displaces_a_scored_one(self):
        alerts = [_alert(f"scoreless[{i}]", confidence=None) for i in range(60)]
        alerts.append(_alert("scored", confidence=0.2))
        section = _section("1", alerts)
        section_data: dict[str, Any] = {}

        set_section_alerts(section_data, section)

        names = [e["attributeName"] for e in section_data["ConfidenceThresholdAlerts"]]
        assert "scored" in names

    def test_no_alerts_writes_no_key(self):
        """Absent must stay absent: this writer replaces the whole section map, so
        writing an empty list where there was no key changes the item."""
        section_data: dict[str, Any] = {}

        set_section_alerts(section_data, _section("1", []))

        assert section_data == {}

    def test_the_serializer_shape_is_unchanged(self):
        section = _section("1", [_alert("Vendor", confidence=0.25)])

        stored = serialize_confidence_threshold_alerts(section)

        assert set(stored[0]) == {
            "attributeName",
            "confidence",
            "confidenceThreshold",
        }
        assert str(stored[0]["confidence"]) == "0.25"  # Decimal, not float


class TestTheDocumentBudget:
    def test_a_document_within_budget_gets_everything(self):
        sections = [_section(str(i), [_alert(f"a{i}")] * 3) for i in range(5)]

        assert allocate_alert_budget(sections) == {str(i): 3 for i in range(5)}

    def test_the_budget_is_never_exceeded(self):
        """The invariant the item ceiling depends on."""
        sections = [_section(str(i), [_alert("x")] * 900) for i in range(40)]

        allotted = allocate_alert_budget(sections)

        assert sum(allotted.values()) <= _MAX_STORED_ALERTS_PER_DOCUMENT

    def test_small_sections_keep_everything_and_donate_the_rest(self):
        """Fair-share: a section wanting 2 of a 10 budget keeps 2, and the section
        wanting 100 gets the remainder rather than a flat half."""
        sections = [
            _section("small", [_alert("s")] * 2),
            _section("big", [_alert("b")] * 100),
        ]

        allotted = allocate_alert_budget(sections, total=10)

        assert allotted == {"small": 2, "big": 8}

    def test_equal_demands_split_evenly(self):
        sections = [_section(str(i), [_alert("x")] * 50) for i in range(4)]

        allotted = allocate_alert_budget(sections, total=100)

        assert allotted == {"0": 25, "1": 25, "2": 25, "3": 25}

    def test_more_alerting_sections_than_budget_still_respects_it(self):
        """Degenerate but must not breach: some sections get zero, and their true
        counts are still recorded by ``set_section_alerts``."""
        sections = [_section(str(i), [_alert("x")] * 5) for i in range(20)]

        allotted = allocate_alert_budget(sections, total=3)

        assert sum(allotted.values()) <= 3

    def test_allocation_is_deterministic(self):
        """Two identical documents must allot identically — the write must not
        depend on dict iteration order."""
        make = lambda: [  # noqa: E731 - terse fixture builder
            _section("a", [_alert("x")] * 700),
            _section("b", [_alert("y")] * 700),
            _section("c", [_alert("z")] * 4),
        ]

        assert allocate_alert_budget(make()) == allocate_alert_budget(make())

    def test_no_sections_is_empty(self):
        assert allocate_alert_budget([]) == {}


class TestTheTwoLayersTogether:
    def test_the_section_cap_still_applies_under_a_generous_budget(self):
        """The document budget can never raise the per-section cap: the atomic
        single-section writer relies on that cap alone."""
        section = _section("1", [_alert(f"row[{i}]") for i in range(500)])
        section_data: dict[str, Any] = {}

        set_section_alerts(section_data, section, limit=_MAX_STORED_ALERTS_PER_DOCUMENT)

        assert (
            len(section_data["ConfidenceThresholdAlerts"])
            == _MAX_STORED_ALERTS_PER_SECTION
        )

    def test_a_tight_budget_lowers_the_section_cap(self):
        section = _section("1", [_alert(f"row[{i}]") for i in range(500)])
        section_data: dict[str, Any] = {}

        set_section_alerts(section_data, section, limit=7)

        assert len(section_data["ConfidenceThresholdAlerts"]) == 7
        assert section_data["ConfidenceThresholdAlertsTotal"] == 500

    def test_a_zero_budget_stores_none_but_records_the_count(self):
        section = _section("1", [_alert("row[0]")])
        section_data: dict[str, Any] = {}

        set_section_alerts(section_data, section, limit=0)

        assert section_data["ConfidenceThresholdAlerts"] == []
        assert section_data["ConfidenceThresholdAlertsTotal"] == 1


class TestThroughTheRealWriters:
    """The helpers being right is not the same as the writers using them."""

    def _service(self):
        return DocumentDynamoDBService(dynamodb_client=Mock())

    def _document(self, sections: int, alerts_each: int) -> Document:
        document = Document(id="doc.pdf", input_key="doc.pdf", status=Status.COMPLETED)
        document.sections = [
            _section(str(i), [_alert(f"s{i}.row[{j}]") for j in range(alerts_each)])
            for i in range(sections)
        ]
        return document

    def _stored_alert_count(self, sections_data: list[dict[str, Any]]) -> int:
        return sum(
            len(section.get("ConfidenceThresholdAlerts", []))
            for section in sections_data
        )

    def test_the_live_item_writer_bounds_the_whole_document(self):
        """20 sections x 400 alerts is ~8,000 alerts, ~880 KB — twice the item
        ceiling, and the write would fail with no result recorded."""
        _, _, values = self._service()._document_to_update_expressions(
            self._document(sections=20, alerts_each=400)
        )

        sections_data = values[":Sections"]
        assert (
            self._stored_alert_count(sections_data) <= _MAX_STORED_ALERTS_PER_DOCUMENT
        )
        # And every truncated section still says how many it really had.
        assert all(
            section["ConfidenceThresholdAlertsTotal"] == 400
            for section in sections_data
        )

    def test_the_live_item_writer_leaves_an_ordinary_document_alone(self):
        _, _, values = self._service()._document_to_update_expressions(
            self._document(sections=3, alerts_each=5)
        )

        sections_data = values[":Sections"]
        assert self._stored_alert_count(sections_data) == 15
        assert not any(
            "ConfidenceThresholdAlertsTotal" in section for section in sections_data
        )

    def test_the_atomic_section_writer_bounds_its_one_section(self):
        """This path cannot see the document, so the per-section cap is the only
        thing standing between it and an oversized item."""
        service = self._service()
        section = _section("1", [_alert(f"row[{i}]") for i in range(4096)])

        service.update_document_section("doc.pdf", 0, section)

        (kwargs,) = [c.kwargs for c in service.client.update_item.call_args_list] or [
            {}
        ]
        stored = kwargs["expression_attribute_values"][":section"]
        assert (
            len(stored["ConfidenceThresholdAlerts"]) == _MAX_STORED_ALERTS_PER_SECTION
        )
        assert stored["ConfidenceThresholdAlertsTotal"] == 4096
