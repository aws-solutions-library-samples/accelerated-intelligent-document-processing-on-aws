# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Multi-instance section recovery (GitHub #565 / #687).

When one section holds several consecutive documents of the SAME class — because
classification found no type change to split on — the model returns a JSON array
where the class schema describes a single object.

The old behaviour stored that array under a ``raw_array`` key that **nothing ever
read**, marked the section FAILED, and reported a misleading
``extraction_sparse`` ("0/N schema fields populated"). Every record had been
extracted correctly and all of them were thrown away.

Now: the first instance becomes ``inference_result`` (so the output shape is
unchanged for every downstream consumer), all instances are preserved, the
section carries an ``instance_count``, and an
``extraction_multi_instance_detected`` warning makes it reviewable.
"""

from __future__ import annotations

import pytest

from idp_common.config.models import IDPConfig
from idp_common.extraction.service import ExtractionService
from idp_common.models import Document, Section


def _svc(*, agentic: bool = False) -> ExtractionService:
    cfg = IDPConfig(**{"extraction": {"agentic": {"enabled": agentic}}})
    svc = ExtractionService(config=cfg)
    svc._class_schema = {
        "type": "object",
        "properties": {
            "patient_name": {"type": "string"},
            "patient_dob": {"type": "string"},
        },
    }
    svc._class_label = "patient_demographics"
    svc._pending_extraction_model = "us.anthropic.claude-sonnet-5"
    return svc


# --------------------------------------------------------------------------
# _normalize_list_result
# --------------------------------------------------------------------------


def test_non_list_passes_through_untouched():
    obj = {"patient_name": "Anderson"}
    fields, ok, count, recovered = ExtractionService._normalize_list_result(
        obj, context="t"
    )
    assert fields is obj
    assert ok is True
    assert count == 0
    assert recovered is None


def test_single_element_array_is_unwrapped():
    fields, ok, count, recovered = ExtractionService._normalize_list_result(
        [{"patient_name": "Anderson"}], context="t"
    )
    assert fields == {"patient_name": "Anderson"}
    assert ok is True
    assert count == 1
    assert recovered is None


@pytest.mark.parametrize("n", [2, 3, 7])
def test_multi_element_array_preserves_every_instance(n):
    records = [
        {"patient_name": f"P{i}", "patient_dob": f"19{70 + i}-01-01"} for i in range(n)
    ]
    fields, ok, count, recovered = ExtractionService._normalize_list_result(
        records, context="t"
    )
    # First instance becomes the section result -> shape unchanged downstream.
    assert fields == records[0]
    # Parsing SUCCEEDED: the data is real and usable. This is the behaviour
    # change -- it used to be reported as a failure.
    assert ok is True
    assert count == n
    # Nothing discarded, order preserved.
    assert recovered == records
    assert len(recovered) == n


def test_empty_array_is_still_a_parse_failure():
    fields, ok, count, recovered = ExtractionService._normalize_list_result(
        [], context="t"
    )
    assert ok is False
    assert count == 0
    assert recovered is None
    assert "error" in fields


def test_array_of_non_objects_is_a_parse_failure_not_a_multi_instance_result():
    """A list of scalars is malformed output, not several documents."""
    fields, ok, count, recovered = ExtractionService._normalize_list_result(
        ["Anderson", "Baker"], context="t"
    )
    assert ok is False
    assert count == 0
    assert recovered is None
    assert "error" in fields


def test_mixed_array_is_rejected_rather_than_partially_accepted():
    fields, ok, count, recovered = ExtractionService._normalize_list_result(
        [{"patient_name": "Anderson"}, "Baker"], context="t"
    )
    assert ok is False
    assert recovered is None
    assert "not objects" in fields["error"]


def test_no_raw_array_key_is_ever_emitted():
    """The dead `raw_array` key is gone; recovered data has a real home."""
    for payload in ([], ["a"], [{"x": 1}, {"x": 2}]):
        fields, _, _, _ = ExtractionService._normalize_list_result(payload, context="t")
        assert "raw_array" not in fields


# --------------------------------------------------------------------------
# ProcessingIssue emission
# --------------------------------------------------------------------------


def test_multi_instance_emits_warning_naming_the_count_and_class():
    svc = _svc()
    issues = svc._build_extraction_issues(
        extracted_fields={"patient_name": "Anderson", "patient_dob": "1970-01-01"},
        metadata={"instance_count": 3},
        section_id="1",
    )
    issue = next(i for i in issues if i.code == "extraction_multi_instance_detected")
    assert issue.severity == "warning"
    assert issue.stage == "extraction"
    assert issue.section_id == "1"
    assert "3" in issue.message
    assert "patient_demographics" in issue.message
    assert issue.details["instance_count"] == 3


def test_single_instance_emits_no_multi_instance_issue():
    svc = _svc()
    for count in (0, 1):
        issues = svc._build_extraction_issues(
            extracted_fields={"patient_name": "Anderson"},
            metadata={"instance_count": count},
            section_id="1",
        )
        assert not [i for i in issues if i.code == "extraction_multi_instance_detected"]


def test_multi_instance_no_longer_reports_the_misleading_sparse_issue():
    """The old path marked the section FAILED and claimed 0/N fields populated.

    With the first instance now populating inference_result normally, the
    population heuristic sees real data and the only issue raised is the
    accurate one.
    """
    svc = _svc()
    issues = svc._build_extraction_issues(
        extracted_fields={"patient_name": "Anderson", "patient_dob": "1970-01-01"},
        metadata={"instance_count": 2},
        section_id="1",
    )
    codes = [i.code for i in issues]
    assert codes == ["extraction_multi_instance_detected"]


# --------------------------------------------------------------------------
# Section.instance_count serialization round-trips
# --------------------------------------------------------------------------


def test_section_instance_count_round_trips():
    s = Section(section_id="1", classification="x", instance_count=3)
    assert s.to_dict()["instance_count"] == 3
    assert Section.from_dict(s.to_dict()).instance_count == 3


def test_section_omits_instance_count_when_undetermined():
    """Byte-identical output for sections that never determined a count."""
    s = Section(section_id="1", classification="x")
    assert s.instance_count == 0
    assert "instance_count" not in s.to_dict()
    assert Section.from_dict(s.to_dict()).instance_count == 0


def test_section_from_dict_tolerates_missing_and_null_instance_count():
    assert Section.from_dict({"section_id": "1"}).instance_count == 0
    assert (
        Section.from_dict({"section_id": "1", "instance_count": None}).instance_count
        == 0
    )


def test_document_to_dict_carries_instance_count():
    """Document.to_dict hand-rolls the section dict, so it needs its own test."""
    doc = Document(input_key="k")
    doc.sections = [
        Section(section_id="1", classification="x", instance_count=2),
        Section(section_id="2", classification="y"),
    ]
    payload = doc.to_dict()
    assert payload["sections"][0]["instance_count"] == 2
    assert "instance_count" not in payload["sections"][1]

    restored = Document.from_dict(payload)
    assert restored.sections[0].instance_count == 2
    assert restored.sections[1].instance_count == 0
