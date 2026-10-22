# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``assessment_geometry_incomplete`` and the scalar field (#1520).

The rung incremented its counter inside ``audit_explainability``'s
``isinstance(data_val, list)`` branch only, so it could not fire for a scalar field
at all — the same row/scalar asymmetry as #1474. Measured over 571 real sections:
**no** list leaf was scored-but-box-less, while 495 scalar ones were, across 222
sections. The rung was counting the case that does not arise and was structurally
blind to the one that does.

The two halves ask deliberately different questions, and the asymmetry is the point
rather than an oversight:

- **any** box-less list row reports (unchanged);
- a **scalar** reports only when no populated field in the section carries a box.

Reporting per scalar leaf was measured at 40% of real sections, and the largest
single contributor is ``Language`` — values like "English" — followed by
``LetterType``/``MemoType``/``FormType``, whose value is the model's
characterization of the document rather than text on the page. No box can exist for
those, so their absence is the correct outcome and an info note about it is noise.
"""

import pytest

from idp_common.assessment.batching import audit_explainability

pytestmark = pytest.mark.unit

GEOMETRY = [{"boundingBox": {"left": 0.1, "top": 0.2, "width": 0.3, "height": 0.02}}]


def _scored(geometry=None):
    leaf = {"confidence": 0.95, "confidence_threshold": 0.8}
    if geometry is not None:
        leaf["geometry"] = geometry
    return leaf


def _codes(issues):
    return [i.code for i in issues]


def _issue(issues, code):
    return next(i for i in issues if i.code == code)


class TestTheScalarHalfCanFireAtAll:
    """Before #1520 no scalar-only section could produce this code."""

    def test_a_section_whose_scalars_are_all_box_less_reports(self):
        extraction = {"Language": "English", "LetterType": "Request"}
        assessment = {"Language": _scored(), "LetterType": _scored()}
        _gaps, issues = audit_explainability(
            assessment, extraction, geometry_mode="ocr_only", section_id="1"
        )
        assert "assessment_geometry_incomplete" in _codes(issues)

    def test_the_issue_counts_the_scalar_leaves_and_says_so(self):
        extraction = {"Language": "English", "LetterType": "Request"}
        assessment = {"Language": _scored(), "LetterType": _scored()}
        _gaps, issues = audit_explainability(
            assessment, extraction, geometry_mode="ocr_only", section_id="1"
        )
        issue = _issue(issues, "assessment_geometry_incomplete")
        assert issue.severity == "info"
        assert issue.details["missing_geometry_scalars"] == 2
        assert issue.details["missing_geometry_rows"] == 0
        assert issue.details["scalar_leaves_wanting_geometry"] == 2
        assert "2 scored leaf/leaves have no bounding box" in issue.message


class TestTheScalarHalfStaysQuietWhereABoxExists:
    """The threshold is "nothing was located", not "something is missing"."""

    def test_a_partially_grounded_section_does_not_report(self):
        """The 40%-of-sections case: one field has no box, others do.

        This is the measured noise case — a ``Language`` or ``LetterType`` whose
        value is nowhere on the page sitting beside fields that grounded fine.
        """
        extraction = {"Title": "Annual Report", "Language": "English"}
        assessment = {"Title": _scored(GEOMETRY), "Language": _scored()}
        _gaps, issues = audit_explainability(
            assessment, extraction, geometry_mode="ocr_only", section_id="1"
        )
        assert "assessment_geometry_incomplete" not in _codes(issues)

    def test_a_fully_grounded_section_does_not_report(self):
        extraction = {"Title": "Annual Report"}
        assessment = {"Title": _scored(GEOMETRY)}
        _gaps, issues = audit_explainability(
            assessment, extraction, geometry_mode="ocr_only", section_id="1"
        )
        assert "assessment_geometry_incomplete" not in _codes(issues)

    def test_a_section_of_only_empty_fields_reports_nothing(self):
        """An absent value needs no box, so it cannot be a missing one.

        This is the case the ``_is_populated_value`` guard exists for, and it is
        what makes the "nothing was located" condition mean what it says: with every
        field empty and every leaf box-less, counting them makes missing equal total
        and the section reports that geometry failed when there was nothing to find.
        Extraction emits ``""``/``None`` for "field not found on the page", so this
        is the ordinary shape of a sparse form rather than an edge case (#1474).
        """
        extraction = {"Subject": "", "Notes": None, "Items": []}
        assessment = {
            "Subject": _scored(),
            "Notes": _scored(),
            "Items": _scored(),
        }
        _gaps, issues = audit_explainability(
            assessment, extraction, geometry_mode="ocr_only", section_id="1"
        )
        assert "assessment_geometry_incomplete" not in _codes(issues)

    def test_an_empty_field_is_not_counted_beside_a_populated_one(self):
        """The count in the message is over populated fields only."""
        extraction = {"Title": "Annual Report", "Subject": "", "Notes": None}
        assessment = {
            "Title": _scored(),
            "Subject": _scored(),
            "Notes": _scored(),
        }
        _gaps, issues = audit_explainability(
            assessment, extraction, geometry_mode="ocr_only", section_id="1"
        )
        issue = _issue(issues, "assessment_geometry_incomplete")
        assert issue.details["missing_geometry_scalars"] == 1
        assert issue.details["scalar_leaves_wanting_geometry"] == 1

    def test_geometry_off_reports_nothing(self):
        extraction = {"Language": "English"}
        assessment = {"Language": _scored()}
        for mode in ("off", None):
            _gaps, issues = audit_explainability(
                assessment, extraction, geometry_mode=mode, section_id="1"
            )
            assert "assessment_geometry_incomplete" not in _codes(issues)

    def test_an_unscored_field_is_not_a_geometry_finding(self):
        """A field with no confidence is #1474's finding, not this one.

        Both could otherwise fire for the same field and double-count the section's
        processing-issue total.
        """
        extraction = {"Title": "Annual Report"}
        assessment = {}
        _gaps, issues = audit_explainability(
            assessment, extraction, geometry_mode="ocr_only", section_id="1"
        )
        codes = _codes(issues)
        assert "assessment_fields_unscored" in codes
        assert "assessment_geometry_incomplete" not in codes


class TestTheListHalfIsUnchanged:
    """The list condition keeps firing on ANY box-less row."""

    def test_one_box_less_row_among_grounded_rows_still_reports(self):
        extraction = {"Items": [{"Name": "a"}, {"Name": "b"}]}
        assessment = {
            "Items": [{"Name": _scored(GEOMETRY)}, {"Name": _scored()}],
        }
        _gaps, issues = audit_explainability(
            assessment, extraction, geometry_mode="ocr_only", section_id="1"
        )
        issue = _issue(issues, "assessment_geometry_incomplete")
        assert issue.details["missing_geometry_rows"] == 1

    def test_a_grounded_list_beside_a_box_less_scalar_reports_the_row_only(self):
        """The two halves compose without either masking the other."""
        extraction = {"Items": [{"Name": "a"}], "Language": "English"}
        assessment = {"Items": [{"Name": _scored()}], "Language": _scored(GEOMETRY)}
        _gaps, issues = audit_explainability(
            assessment, extraction, geometry_mode="ocr_only", section_id="1"
        )
        issue = _issue(issues, "assessment_geometry_incomplete")
        assert issue.details["missing_geometry_rows"] == 1
        assert issue.details["missing_geometry_scalars"] == 0
