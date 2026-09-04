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


def _svc(*, agentic: bool = False, detection: bool = True) -> ExtractionService:
    # Detection is OFF by default as shipped (gated on the benchmark A/B), so the
    # tests that exercise it enable it explicitly rather than relying on the
    # default — which is what a test should pin anyway.
    cfg = IDPConfig(
        **{
            "extraction": {
                "agentic": {"enabled": agentic},
                "multi_instance_detection": {"enabled": detection},
            }
        }
    )
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
    cfg = IDPConfig(
        **{
            "extraction": {
                "agentic": {"enabled": False},
                "multi_instance_detection": {"enabled": True},
            }
        }
    )
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


# --------------------------------------------------------------------------
# instance_source labelling
#
# Caught by reading a live e2e audit trail: every ordinary single-object
# extraction was labelled source="recovered", which is untrue — nothing was
# recovered — and would send someone reading the metadata looking for records
# that never existed.
# --------------------------------------------------------------------------


def test_single_object_is_labelled_single_not_recovered():
    svc = _svc()
    issues = svc._build_extraction_issues(
        extracted_fields={"patient_name": "Anderson"},
        metadata={"instance_count": 1, "instance_source": "single"},
        section_id="1",
    )
    # A single instance never warrants the multi-instance warning.
    assert not [i for i in issues if i.code == "extraction_multi_instance_detected"]


def test_declared_source_still_suppresses_the_warning():
    svc = _packet_svc()
    issues = svc._build_extraction_issues(
        extracted_fields={"records": [{"patient_name": "A"}, {"patient_name": "B"}]},
        metadata={"instance_count": 2, "instance_source": "declared"},
        section_id="1",
    )
    assert not [i for i in issues if i.code == "extraction_multi_instance_detected"]


def test_recovered_source_is_the_only_one_that_warns():
    svc = _svc()
    for source, expect_warning in (
        ("recovered", True),
        ("declared", False),
        ("single", False),
    ):
        issues = svc._build_extraction_issues(
            extracted_fields={"patient_name": "A", "patient_dob": "1970-01-01"},
            metadata={"instance_count": 2, "instance_source": source},
            section_id="1",
        )
        got = bool(
            [i for i in issues if i.code == "extraction_multi_instance_detected"]
        )
        assert got is expect_warning, f"source={source} -> warning={got}"


# --------------------------------------------------------------------------
# #753 — the SILENT case: the model returned ONE object for a section that
# holds several documents. Nothing anywhere reported it: SUCCESS, COMPLETED,
# ProcessingIssueCount 0, instance_count 1. Detection asks the model, in the
# same inference, how many documents the pages hold.
# --------------------------------------------------------------------------


def test_suspected_multi_instance_warns_naming_both_counts():
    svc = _svc()
    issues = svc._build_extraction_issues(
        extracted_fields={"patient_name": "Anderson", "patient_dob": "1970-01-01"},
        metadata={
            "instance_count": 3,
            "instance_source": "suspected",
            "instance_probe": 3,
            "instance_extracted_count": 1,
        },
        section_id="1",
    )
    issue = next(i for i in issues if i.code == "extraction_multi_instance_suspected")
    assert issue.severity == "warning"
    assert issue.stage == "extraction"
    assert issue.section_id == "1"
    # Both numbers, so the message cannot be misread as "3 records extracted".
    assert "3 separate" in issue.message
    assert "returned 1" in issue.message
    assert "2 document(s) are NOT in the result" in issue.message
    assert issue.details["instance_count"] == 3
    assert issue.details["extracted_instance_count"] == 1
    assert issue.details["detection"] == "model_self_report"


def test_suspected_does_not_also_raise_the_recovered_warning():
    """Two warnings for one situation would be noise, and the 'detected' wording
    ("all N were extracted") is FALSE for the suspected case."""
    svc = _svc()
    codes = [
        i.code
        for i in svc._build_extraction_issues(
            extracted_fields={"patient_name": "A", "patient_dob": "1970-01-01"},
            metadata={
                "instance_count": 2,
                "instance_source": "suspected",
                "instance_extracted_count": 1,
            },
            section_id="1",
        )
    ]
    assert codes == ["extraction_multi_instance_suspected"]


def test_probe_of_one_is_the_normal_case_and_raises_nothing():
    """A genuine single-document section must stay silent — a warning that fires
    on ordinary documents is worse than none (#753 acceptance criteria)."""
    svc = _svc()
    issues = svc._build_extraction_issues(
        extracted_fields={"patient_name": "A", "patient_dob": "1970-01-01"},
        metadata={
            "instance_count": 1,
            "instance_source": "single",
            "instance_probe": 1,
        },
        section_id="1",
    )
    assert issues == []


def test_suspected_needs_a_count_above_one():
    svc = _svc()
    issues = svc._build_extraction_issues(
        extracted_fields={"patient_name": "A", "patient_dob": "1970-01-01"},
        metadata={"instance_count": 1, "instance_source": "suspected"},
        section_id="1",
    )
    assert not [i for i in issues if i.code == "extraction_multi_instance_suspected"]


# --------------------------------------------------------------------------
# Probe plumbing through _save_results: the wire schema carries the probe, the
# class schema never does, and the count/labelling stays honest.
# --------------------------------------------------------------------------


def test_wire_schema_carries_the_probe_but_the_class_schema_does_not():
    from idp_common.extraction.instance_probe import INSTANCE_PROBE_FIELD

    svc = _svc()
    wire, added = svc._build_wire_schema(svc._class_schema, "patient_demographics")
    assert added is True
    assert INSTANCE_PROBE_FIELD in wire["properties"]
    assert INSTANCE_PROBE_FIELD not in svc._class_schema["properties"]


def test_probe_is_not_requested_when_detection_is_disabled():
    svc = _svc(detection=False)
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    wire, added = svc._build_wire_schema(schema, "c")
    assert added is False
    assert wire is schema


def test_detection_is_OFF_by_default():
    """Gated on the benchmark A/B: completeness and cost were unchanged, but scalar
    accuracy was consistently a little worse with it on. Pinning the DEFAULT here
    so it cannot drift back on without someone re-measuring."""
    assert IDPConfig().extraction.multi_instance_detection.enabled is False
    svc = ExtractionService(config=IDPConfig())
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    wire, added = svc._build_wire_schema(schema, "c")
    assert added is False
    assert wire is schema


def test_probe_is_not_requested_in_agentic_mode():
    """Advanced extraction validates through a generated Pydantic model and shards
    by field, so an auxiliary property would be dropped on some paths and
    duplicated on others. Documented gap, not an oversight."""
    svc = _svc(agentic=True)
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    wire, added = svc._build_wire_schema(schema, "c")
    assert added is False
    assert wire is schema


def test_read_instance_probe_strips_the_field():
    from idp_common.extraction.instance_probe import INSTANCE_PROBE_FIELD

    svc = _svc()
    fields = {"patient_name": "A", INSTANCE_PROBE_FIELD: 3}
    assert svc._read_instance_probe(fields) == 3
    assert fields == {"patient_name": "A"}


# --------------------------------------------------------------------------
# _resolve_instance_reporting — the whole multi-instance reporting contract,
# testable without S3.
# --------------------------------------------------------------------------


def _result(**kw):
    from idp_common.extraction.service import ExtractionResult

    defaults = dict(
        extracted_fields={"patient_name": "A"},
        metering={},
        parsing_succeeded=True,
        total_duration=1.0,
        instance_count=1,
    )
    defaults.update(kw)
    return ExtractionResult(**defaults)


def test_reporting_ordinary_single_object():
    count, meta = _svc()._resolve_instance_reporting(_result())
    assert count == 1
    assert meta == {"instance_count": 1, "instance_source": "single"}


def test_reporting_recovered_array():
    recovered = [{"patient_name": "A"}, {"patient_name": "B"}]
    count, meta = _svc()._resolve_instance_reporting(
        _result(instance_count=2, recovered_instances=recovered)
    )
    assert count == 2
    assert meta["instance_source"] == "recovered"


def test_reporting_declared_axis_wins_over_the_response_shape():
    count, meta = _packet_svc()._resolve_instance_reporting(
        _result(extracted_fields={"records": [{}, {}, {}]})
    )
    assert count == 3
    assert meta["instance_source"] == "declared"


def test_reporting_suspected_records_both_counts():
    count, meta = _svc()._resolve_instance_reporting(_result(instance_probe=3))
    assert count == 3
    assert meta["instance_source"] == "suspected"
    assert meta["instance_extracted_count"] == 1
    assert meta["instance_probe"] == 3


def test_reporting_probe_agreeing_with_the_result_is_not_suspected():
    count, meta = _svc()._resolve_instance_reporting(_result(instance_probe=1))
    assert count == 1
    assert meta["instance_source"] == "single"
    # Still recorded for audit — "the model was asked and said 1" is useful.
    assert meta["instance_probe"] == 1
    assert "instance_extracted_count" not in meta


def test_reporting_probe_below_the_recovered_count_does_not_lower_it():
    """The records are physically present; the model's guess does not outvote
    them."""
    recovered = [{"a": 1}, {"a": 2}, {"a": 3}]
    count, meta = _svc()._resolve_instance_reporting(
        _result(instance_count=3, recovered_instances=recovered, instance_probe=2)
    )
    assert count == 3
    assert meta["instance_source"] == "recovered"


def test_reporting_a_declared_packet_that_under_extracted_is_still_flagged():
    """Designate/Synthesize mode is not a free pass: 1 record extracted where the
    model says there are 3 is the same data loss."""
    count, meta = _packet_svc()._resolve_instance_reporting(
        _result(extracted_fields={"records": [{}]}, instance_probe=3)
    )
    assert count == 3
    assert meta["instance_source"] == "suspected"
    assert meta["instance_extracted_count"] == 1


def test_reporting_undetermined_stays_zero_and_emits_nothing():
    count, meta = _svc()._resolve_instance_reporting(
        _result(
            instance_count=0, parsing_succeeded=False, extracted_fields={"error": "x"}
        )
    )
    assert count == 0
    assert meta == {}


# --------------------------------------------------------------------------
# Synthesize mode: rescuing a response that ignored the wrapper (#715).
#
# The requested shape is {"instances": [...]}. A bare array or a single flat
# record would otherwise be deleted entirely by the off-schema filter — whose
# only allowed top-level key is now `instances` — emitting {}. Total data loss
# from a response that contained the data.
# --------------------------------------------------------------------------


def _wrapped_svc() -> ExtractionService:
    from idp_common.schema.multi_instance import wrap_class_schema

    cfg = IDPConfig(
        **{
            "extraction": {
                "agentic": {"enabled": False},
                "multi_instance_detection": {"enabled": True},
            }
        }
    )
    svc = ExtractionService(config=cfg)
    svc._class_schema = wrap_class_schema(
        {
            "$id": "Pay-Statement",
            "type": "object",
            "x-aws-idp-multi-instance": True,
            "properties": {
                "CheckNumber": {"type": "string"},
                "NetPay": {"type": "string"},
            },
        }
    )
    svc._class_label = "Pay-Statement"
    return svc


def test_a_bare_array_becomes_the_instance_list():
    svc = _wrapped_svc()
    records = [{"CheckNumber": "1"}, {"CheckNumber": "2"}, {"CheckNumber": "3"}]
    fields, recovered = svc._adapt_to_instances_wrapper(records[0], records)
    assert fields == {"instances": records}
    # No longer "recovered" — they are the result now.
    assert recovered is None


def test_a_single_flat_record_becomes_one_instance():
    svc = _wrapped_svc()
    fields, recovered = svc._adapt_to_instances_wrapper({"CheckNumber": "1"}, None)
    assert fields == {"instances": [{"CheckNumber": "1"}]}
    assert recovered is None


def test_a_correctly_wrapped_response_is_untouched():
    svc = _wrapped_svc()
    payload = {"instances": [{"CheckNumber": "1"}]}
    fields, recovered = svc._adapt_to_instances_wrapper(payload, None)
    assert fields is payload
    assert recovered is None


def test_an_unflagged_class_is_never_adapted():
    svc = _svc()
    records = [{"patient_name": "A"}, {"patient_name": "B"}]
    fields, recovered = svc._adapt_to_instances_wrapper(records[0], records)
    assert fields == records[0]
    assert recovered == records


def test_a_parse_failure_sentinel_is_not_adopted_as_a_record():
    """`{"error": ...}` / `{"raw_output": ...}` must stay a parse failure, not
    become a bogus instance."""
    svc = _wrapped_svc()
    for sentinel in ({"error": "boom"}, {"raw_output": "prose"}):
        fields, _ = svc._adapt_to_instances_wrapper(sentinel, None)
        assert fields is sentinel


def test_an_empty_response_is_not_turned_into_an_instance():
    svc = _wrapped_svc()
    fields, _ = svc._adapt_to_instances_wrapper({}, None)
    assert fields == {}


# --------------------------------------------------------------------------
# The probe must not survive inside ANY instance.
#
# Found in review: the strip loop ran BEFORE _adapt_to_instances_wrapper and
# only over `recovered_instances`. For a flagged class answering with a bare
# array the adapt moves the records into extracted_fields["instances"] and
# returns recovered_instances=None, so the loop never ran — and
# _filter_extracted_to_schema only filters TOP-LEVEL keys, so the probe survived
# inside inference_result["instances"][1..N]. Element 0 looked clean because it
# is ALIASED to extracted_fields, which is exactly why testing the adapt in
# isolation passed.
# --------------------------------------------------------------------------


def test_probe_is_stripped_from_every_instance_of_a_wrapped_result():
    from idp_common.extraction.instance_probe import INSTANCE_PROBE_FIELD

    svc = _wrapped_svc()
    records = [
        {"CheckNumber": "1", INSTANCE_PROBE_FIELD: 3},
        {"CheckNumber": "2", INSTANCE_PROBE_FIELD: 3},
        {"CheckNumber": "3", INSTANCE_PROBE_FIELD: 3},
    ]
    fields = {"instances": records}
    svc._strip_probe_from_instances(fields, None)
    assert all(INSTANCE_PROBE_FIELD not in r for r in fields["instances"])
    assert [r["CheckNumber"] for r in fields["instances"]] == ["1", "2", "3"]


def test_probe_is_stripped_from_recovered_instances_of_an_unflagged_class():
    from idp_common.extraction.instance_probe import INSTANCE_PROBE_FIELD

    svc = _svc()
    recovered = [
        {"patient_name": "A", INSTANCE_PROBE_FIELD: 2},
        {"patient_name": "B", INSTANCE_PROBE_FIELD: 2},
    ]
    svc._strip_probe_from_instances(recovered[0], recovered)
    assert all(INSTANCE_PROBE_FIELD not in r for r in recovered)


def test_the_adapt_then_strip_ORDER_leaves_no_probe_anywhere():
    """The end-to-end ordering, which is the thing that was actually wrong.

    A bare array from a flagged class: adapt moves it under `instances` and drops
    `recovered_instances`, so the sweep must happen AFTER the adapt and must look
    at the wrapper.
    """
    from idp_common.extraction.instance_probe import INSTANCE_PROBE_FIELD

    svc = _wrapped_svc()
    records = [
        {"CheckNumber": "1", INSTANCE_PROBE_FIELD: 3},
        {"CheckNumber": "2", INSTANCE_PROBE_FIELD: 3},
        {"CheckNumber": "3", INSTANCE_PROBE_FIELD: 3},
    ]
    # What the parse path produces for a bare array: fields aliases element 0.
    extracted, recovered = svc._normalize_list_result(records, context="t")[0], records
    assert svc._read_instance_probe(extracted) == 3  # the top-level pop
    extracted, recovered = svc._adapt_to_instances_wrapper(extracted, recovered)
    svc._strip_probe_from_instances(extracted, recovered)

    assert extracted["instances"] and len(extracted["instances"]) == 3
    for record in extracted["instances"]:
        assert INSTANCE_PROBE_FIELD not in record, (
            "the probe survived inside an instance — it would be scored by "
            "assessment, written to a reporting column and diffed against a "
            "baseline that has no such key"
        )


def test_strip_tolerates_missing_and_malformed_containers():
    svc = _wrapped_svc()
    svc._strip_probe_from_instances({}, None)
    svc._strip_probe_from_instances({"instances": "not-a-list"}, None)
    svc._strip_probe_from_instances(None, None)
    svc._strip_probe_from_instances({"instances": [None, "x", {"a": 1}]}, None)


def test_a_count_that_arrives_ONLY_inside_records_is_still_used():
    """The value is not thrown away just because it turned up in the wrong place.

    A model that answers with the count inside each record and never at the top
    level would otherwise leave instance_probe unset and fire no warning at all.
    """
    from idp_common.extraction.instance_probe import INSTANCE_PROBE_FIELD

    svc = _wrapped_svc()
    fields = {
        "instances": [
            {"CheckNumber": "1", INSTANCE_PROBE_FIELD: 3},
            {"CheckNumber": "2", INSTANCE_PROBE_FIELD: 3},
        ]
    }
    assert svc._read_instance_probe(fields) is None  # nothing at the top level
    assert svc._strip_probe_from_instances(fields, None) == 3
    assert all(INSTANCE_PROBE_FIELD not in r for r in fields["instances"])


def test_the_largest_nested_count_wins():
    from idp_common.extraction.instance_probe import INSTANCE_PROBE_FIELD

    svc = _wrapped_svc()
    fields = {
        "instances": [
            {"CheckNumber": "1", INSTANCE_PROBE_FIELD: 2},
            {"CheckNumber": "2", INSTANCE_PROBE_FIELD: 3},
        ]
    }
    assert svc._strip_probe_from_instances(fields, None) == 3


def test_no_nested_count_returns_none():
    svc = _wrapped_svc()
    assert (
        svc._strip_probe_from_instances({"instances": [{"CheckNumber": "1"}]}, None)
        is None
    )
