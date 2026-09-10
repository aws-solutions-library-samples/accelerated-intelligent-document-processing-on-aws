# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The recorded alert count must describe the list that was actually stored.

``_attach_explainability`` dedupes the integrated path's alerts before assigning
them to the section (#814/#815), but recorded ``assessment_alert_count`` from the
PRE-dedupe list — so a section carrying 16 alerts reported 2,603, and the number
contradicted the data beside it. The raw figure is still worth having when it
differs, because the gap IS the duplication this path removes, so it now travels
under its own name instead of displacing the real count (#822).
"""

from typing import Any

import pytest

from idp_common.extraction.service import ExtractionService, SectionInfo
from idp_common.models import Document

pytestmark = pytest.mark.unit

SCHEMA: dict[str, Any] = {
    "type": "object",
    "$id": "Invoice",
    "properties": {
        "InvoiceTotal": {"type": "string"},
        "Vendor": {"type": "string"},
    },
}


class _Section:
    """Only the attribute the method under test writes."""

    def __init__(self) -> None:
        self.confidence_threshold_alerts: list[dict[str, Any]] = []


def _service() -> ExtractionService:
    svc = ExtractionService(
        region="us-west-2",
        config={
            # geometry off: the grounding branch needs real OCR pages, and it is
            # not what these tests are about.
            "extraction": {
                "model": "us.amazon.nova-pro-v1:0",
                "geometry": {"mode": "off"},
            },
            "classes": [SCHEMA],
        },
    )
    svc._class_schema = SCHEMA
    # The missing-row retry is a separate concern with its own tests; disabling it
    # keeps the alert list exactly what this test hands in.
    svc._integrated_assessment_enabled = lambda: False  # type: ignore[method-assign]
    return svc


def _section_info() -> SectionInfo:
    return SectionInfo(
        class_label="Invoice",
        sorted_page_ids=["1"],
        page_indices=[0],
        output_bucket="bucket",
        output_key="key",
        output_uri="s3://bucket/key",
        start_page=1,
        end_page=1,
    )


def _alert(name: str, confidence: float) -> dict[str, Any]:
    return {
        "attribute_name": name,
        "confidence": confidence,
        "confidence_threshold": 0.8,
    }


def _attach(alerts: list[dict[str, Any]]) -> tuple[dict[str, Any], _Section]:
    svc = _service()
    section = _Section()
    output_metadata: dict[str, Any] = {}
    svc._attach_explainability(
        output_metadata=output_metadata,
        merged_assessment={"InvoiceTotal": {"confidence": 0.4}},
        merged_assessment_alerts=alerts,
        extracted_fields={"InvoiceTotal": "100.00", "Vendor": "Acme"},
        document=Document(id="doc-1", input_key="doc.pdf"),
        section=section,
        section_info=_section_info(),
    )
    return output_metadata, section


def test_the_count_matches_the_stored_alerts_when_duplicates_collapse():
    """163 copies of one finding are stored as one, and counted as one."""
    metadata, section = _attach([_alert("totals.total_overhead", 0.4)] * 163)

    assert len(section.confidence_threshold_alerts) == 1
    assert metadata["assessment_alert_count"] == 1
    assert metadata["assessment_alert_count_before_dedupe"] == 163


def test_the_raw_count_is_absent_when_nothing_was_collapsed():
    """The extra key exists to explain a gap; with no gap it would be noise."""
    metadata, section = _attach([_alert("Vendor", 0.5), _alert("InvoiceTotal", 0.6)])

    assert len(section.confidence_threshold_alerts) == 2
    assert metadata["assessment_alert_count"] == 2
    assert "assessment_alert_count_before_dedupe" not in metadata


def test_indexed_row_alerts_are_all_counted():
    """Per-row alerts are never deduped (indexed paths pass through dedupe_alerts;
    since #813 their indexes are global), so the count must not shrink for them
    either."""
    alerts = [_alert(f"Transactions[{i}].Amount", 0.5) for i in range(40)]

    metadata, section = _attach(alerts)

    assert len(section.confidence_threshold_alerts) == 40
    assert metadata["assessment_alert_count"] == 40
    assert "assessment_alert_count_before_dedupe" not in metadata


def test_no_alerts_records_zero():
    metadata, section = _attach([])

    assert section.confidence_threshold_alerts == []
    assert metadata["assessment_alert_count"] == 0
    assert "assessment_alert_count_before_dedupe" not in metadata


def _service_integrated() -> ExtractionService:
    """Integrated confidence genuinely ON: the missing-row retry helper runs."""
    svc = _service()
    svc._integrated_assessment_enabled = lambda: True  # type: ignore[method-assign]
    return svc


def test_integrated_surface_is_regenerated_when_nothing_needs_retrying():
    """#813 regression guard. The integrated path REPLACES its alert surface with
    what the missing-row retry returns. When every row was already scored inline
    (the common case — and always, for a scalar-only class) the helper has
    nothing to retry; it must still hand back the surface rebuilt from the
    merged assessment, not an empty list that wipes every alert."""
    svc = _service_integrated()
    section = _Section()
    output_metadata: dict[str, Any] = {}
    svc._attach_explainability(
        output_metadata=output_metadata,
        merged_assessment={
            "InvoiceTotal": {"confidence": 0.4},
            "Vendor": {"confidence": 0.95},
        },
        # A slice-local incoming surface that the regeneration must supersede.
        merged_assessment_alerts=[_alert("InvoiceTotal", 0.4)],
        extracted_fields={"InvoiceTotal": "100.00", "Vendor": "Acme"},
        document=Document(id="doc-1", input_key="doc.pdf"),
        section=section,
        section_info=_section_info(),
    )
    names = [a["attribute_name"] for a in section.confidence_threshold_alerts]
    assert names == ["InvoiceTotal"], names
    assert output_metadata["assessment_alert_count"] == 1


def test_integrated_surface_with_a_fully_scored_list_is_globally_indexed():
    """A list whose every row scored inline: nothing to retry, and the regenerated
    surface enumerates the FULL merged list so indexes are global."""
    svc = _service_integrated()
    svc._class_schema = {
        "type": "object",
        "$id": "Statement",
        "properties": {
            "Transactions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"Amount": {"type": "string"}},
                },
            }
        },
    }
    rows = [{"Amount": str(i)} for i in range(30)]
    assessment = {
        "Transactions": [
            {"Amount": {"confidence": 0.3 if i == 28 else 0.99}} for i in range(30)
        ]
    }
    section = _Section()
    output_metadata: dict[str, Any] = {}
    svc._attach_explainability(
        output_metadata=output_metadata,
        merged_assessment=assessment,
        merged_assessment_alerts=[_alert("Transactions[3].Amount", 0.3)],  # slice-local
        extracted_fields={"Transactions": rows},
        document=Document(id="doc-1", input_key="doc.pdf"),
        section=section,
        section_info=_section_info(),
    )
    names = [a["attribute_name"] for a in section.confidence_threshold_alerts]
    assert names == ["Transactions[28].Amount"], names
