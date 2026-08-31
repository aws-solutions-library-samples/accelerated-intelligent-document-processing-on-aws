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
from idp_common.models import Document, Page, Section


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
    """A plain object is one document, and we know it — so count is 1, not 0.

    0 means "not determined" (extraction failed before producing a result, or a
    section written by older code), which the UI renders as "-" rather than a
    count. Reporting 0 for a perfectly good single-record extraction would make
    the common case look undetermined.
    """
    obj = {"patient_name": "Anderson"}
    fields, ok, count, recovered = ExtractionService._normalize_list_result(
        obj, context="t"
    )
    assert fields is obj
    assert ok is True
    assert count == 1
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


# --------------------------------------------------------------------------
# Page.document_boundary persistence
#
# The boundary signal drives every llm_determined merge decision but used to be
# stashed via `setattr(page, "metadata", ...)` on an attribute that is not a
# dataclass field — so it was absent from Document.to_dict, never survived the
# Step Functions hop, and never reached DynamoDB. An unexpected section merge
# could then only be diagnosed from Lambda logs (GitHub #565).
# --------------------------------------------------------------------------


def test_page_document_boundary_round_trips_through_document():
    doc = Document(input_key="k")
    doc.pages["1"] = Page(page_id="1", classification="c", document_boundary="start")
    doc.pages["2"] = Page(page_id="2", classification="c", document_boundary="continue")
    doc.pages["3"] = Page(page_id="3", classification="c")

    payload = doc.to_dict()
    assert payload["pages"]["1"]["document_boundary"] == "start"
    assert payload["pages"]["2"]["document_boundary"] == "continue"
    # Omitted when the model produced no signal -> payload unchanged for pages
    # (and documents) written before this field existed.
    assert "document_boundary" not in payload["pages"]["3"]

    restored = Document.from_dict(payload)
    assert restored.pages["1"].document_boundary == "start"
    assert restored.pages["2"].document_boundary == "continue"
    assert restored.pages["3"].document_boundary is None


def test_page_default_document_boundary_is_none():
    assert Page(page_id="1").document_boundary is None


# --------------------------------------------------------------------------
# Designate mode: x-aws-idp-instance-array
#
# A class already modelled as a PACKET of records names its own instance axis.
# The count then comes from that array's length with NO schema transform and NO
# output-shape change, so configs that already solved multi-record packets by
# hand (the #565 workaround) get instance_count and the UI badge for free.
# --------------------------------------------------------------------------


def _packet_svc(instance_array="records") -> ExtractionService:
    cfg = IDPConfig(**{"extraction": {"agentic": {"enabled": False}}})
    svc = ExtractionService(config=cfg)
    svc._class_schema = {
        "type": "object",
        "x-aws-idp-instance-array": instance_array,
        "properties": {
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"patient_name": {"type": "string"}},
                },
            }
        },
    }
    svc._class_label = "patient_packet"
    return svc


def test_designated_instance_count_uses_declared_array_length():
    svc = _packet_svc()
    fields = {
        "records": [{"patient_name": "A"}, {"patient_name": "B"}, {"patient_name": "C"}]
    }
    assert svc._designated_instance_count(fields) == 3


def test_designated_instance_count_one_record():
    svc = _packet_svc()
    assert svc._designated_instance_count({"records": [{"patient_name": "A"}]}) == 1


def test_designated_instance_count_null_array_is_zero_not_error():
    """Extracted as null = genuinely no records, not a misconfiguration."""
    svc = _packet_svc()
    assert svc._designated_instance_count({"records": None}) == 0


def test_no_declaration_returns_none():
    """The overwhelmingly common case: the class declares no instance axis."""
    svc = _svc()
    assert svc._designated_instance_count({"patient_name": "A"}) is None


def test_designated_instance_count_forgiving_at_runtime():
    """A misconfiguration costs a log line, never an extraction."""
    svc = _packet_svc()
    # Declared property absent from this result.
    assert svc._designated_instance_count({"something_else": []}) is None
    # Declared property is not a list.
    assert svc._designated_instance_count({"records": {"a": 1}}) is None
    # Non-dict result.
    assert svc._designated_instance_count(["not", "a", "dict"]) is None
    # Declaration itself is the wrong type.
    bad = _packet_svc(instance_array=["records"])
    assert bad._designated_instance_count({"records": [{}, {}]}) is None


def test_declared_multi_instance_does_not_raise_the_warning():
    """A declared packet holding N records is CORRECT, not a problem.

    The warning exists for the case where the model returned extra documents
    unexpectedly and only the first is scored. A class that declared its own
    instance axis extracts and scores every record, so warning would be noise.
    """
    svc = _packet_svc()
    issues = svc._build_extraction_issues(
        extracted_fields={"records": [{"patient_name": "A"}, {"patient_name": "B"}]},
        metadata={"instance_count": 2, "instance_source": "declared"},
        section_id="1",
    )
    assert not [i for i in issues if i.code == "extraction_multi_instance_detected"]


def test_recovered_multi_instance_still_raises_the_warning():
    """The unexpected case must still be flagged — contrast with the test above."""
    svc = _svc()
    issues = svc._build_extraction_issues(
        extracted_fields={"patient_name": "A", "patient_dob": "1970-01-01"},
        metadata={"instance_count": 2, "instance_source": "recovered"},
        section_id="1",
    )
    assert [i for i in issues if i.code == "extraction_multi_instance_detected"]
