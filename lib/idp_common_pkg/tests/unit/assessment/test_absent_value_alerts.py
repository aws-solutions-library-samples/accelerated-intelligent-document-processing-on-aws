# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""An absent value raises no confidence alert; an absent REQUIRED field is counted.

#1527. A field whose extracted value is absent (null, blank, empty, or a
container of nothing but those) keeps its score and reason in
``explainability_info`` but leaves ``confidence_threshold_alerts`` — and so the
document's ``ConfidenceAlertCount``, the legacy HITL trigger and the review
reasons, which all read that list. Separately, each section lists the fields its
class schema marks ``required`` that extraction left absent
(``missing_required_fields``), and the document sums them.

The unit tests pin the two rules in ``idp_common.assessment.presence``; the
chokepoint tests drive each place a mode stores its alerts — the standalone
Assessment step, the integrated path in extraction, the BDA result processor —
so removing the filter at any one of them fails here.
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import patch

import pytest

from idp_common.assessment.presence import (
    collect_missing_required_fields,
    exclude_absent_value_alerts,
    has_extracted_value,
    iter_assessed_leaf_values,
)
from idp_common.models import Document, Page, Section, Status

pytestmark = pytest.mark.unit


def _alert(name: str, confidence: float = 0.0, threshold: float = 0.8) -> dict:
    return {
        "attribute_name": name,
        "confidence": confidence,
        "confidence_threshold": threshold,
    }


def _names(entries: list[dict[str, Any]]) -> list[str]:
    return [entry["attribute_name"] for entry in entries]


# --------------------------------------------------------------------------- #
# The absence predicate
# --------------------------------------------------------------------------- #


class TestHasExtractedValue:
    @pytest.mark.parametrize(
        "value",
        [None, "", "   ", [], {}, {"State": None, "Zip": ""}, [None, ""], [{}]],
    )
    def test_absent(self, value):
        assert has_extracted_value(value) is False

    @pytest.mark.parametrize(
        "value", ["x", 0, 0.0, False, {"State": "WA", "Zip": None}, [None, "a"]]
    )
    def test_present(self, value):
        """``0`` and ``False`` are values an extraction can legitimately find."""
        assert has_extracted_value(value) is True


# --------------------------------------------------------------------------- #
# Rule 1: alerts on absent values are dropped
# --------------------------------------------------------------------------- #


class TestExcludeAbsentValueAlerts:
    def test_scalar_absent_values_lose_their_alert_and_present_ones_keep_it(self):
        assessment = {
            "Title": {"confidence": 0.0},
            "Doi": {"confidence": 0.1},
            "Issn": {"confidence": 0.2},
            "Journal": {"confidence": 0.3},
        }
        values = {"Title": None, "Doi": "", "Issn": "  ", "Journal": "Nature"}
        alerts = [_alert(n) for n in ("Title", "Doi", "Issn", "Journal")]

        kept = exclude_absent_value_alerts(alerts, assessment, values)

        assert _names(kept) == ["Journal"]

    def test_a_key_missing_from_the_values_is_absent(self):
        kept = exclude_absent_value_alerts(
            [_alert("Doi")], {"Doi": {"confidence": 0.0}}, {}
        )
        assert kept == []

    def test_nested_group_leaves_are_judged_on_their_own_value(self):
        assessment = {
            "Address": {
                "City": {"confidence": 0.1},
                "Zip": {"confidence": 0.1},
            }
        }
        values = {"Address": {"City": "Seattle", "Zip": None}}
        alerts = [_alert("Address.City"), _alert("Address.Zip")]

        assert _names(exclude_absent_value_alerts(alerts, assessment, values)) == [
            "Address.City"
        ]

    def test_list_cells_are_judged_per_row(self):
        assessment = {
            "Rows": [
                {"Amount": {"confidence": 0.1}, "Memo": {"confidence": 0.1}},
                {"Amount": {"confidence": 0.1}, "Memo": {"confidence": 0.1}},
            ]
        }
        values = {
            "Rows": [{"Amount": "1.00", "Memo": ""}, {"Amount": None, "Memo": "x"}]
        }
        alerts = [
            _alert("Rows[0].Amount"),
            _alert("Rows[0].Memo"),
            _alert("Rows[1].Amount"),
            _alert("Rows[1].Memo"),
        ]
        assert _names(exclude_absent_value_alerts(alerts, assessment, values)) == [
            "Rows[0].Amount",
            "Rows[1].Memo",
        ]

    def test_a_group_level_leaf_over_an_all_null_object_is_absent(self):
        """``_is_populated_value`` alone calls this dict populated; there is
        nothing in it for a score to be about."""
        kept = exclude_absent_value_alerts(
            [_alert("Address")],
            {"Address": {"confidence": 0.0}},
            {"Address": {"City": None, "Zip": ""}},
        )
        assert kept == []

    def test_a_decomposed_string_keeps_its_fragment_alerts(self):
        """The model split one populated string into sub-keyed fragments. There
        is no per-fragment value, so the fragments count as present — the
        conservative direction, today's behaviour."""
        assessment = {
            "Insurer": {"Fake Co": {"confidence": 0.2}, "1 Main": {"confidence": 0.3}}
        }
        values = {"Insurer": "Fake Co, 1 Main"}
        alerts = [_alert("Insurer.Fake Co"), _alert("Insurer.1 Main")]
        assert exclude_absent_value_alerts(alerts, assessment, values) == alerts

    def test_an_alert_beneath_an_absent_leaf_is_dropped(self):
        """``AssessmentService._check_confidence_alerts`` reports a leaf's own
        sub-fields too; a value under an absent value is absent."""
        assessment = {"Total": {"confidence": 0.0, "Cents": {"confidence": 0.1}}}
        kept = exclude_absent_value_alerts(
            [_alert("Total"), _alert("Total.Cents")], assessment, {"Total": None}
        )
        assert kept == []

    def test_a_prefix_is_matched_only_at_a_path_boundary(self):
        assessment = {"Total": {"confidence": 0.0}, "Total_due": {"confidence": 0.1}}
        values = {"Total": None, "Total_due": "5"}
        kept = exclude_absent_value_alerts(
            [_alert("Total"), _alert("Total_due")], assessment, values
        )
        assert _names(kept) == ["Total_due"]

    @pytest.mark.parametrize(
        ("assessment", "values"),
        [(None, {"A": None}), ({"A": {"confidence": 0}}, None), ([], {}), ({}, [])],
    )
    def test_nothing_is_dropped_when_there_is_nothing_to_judge_against(
        self, assessment, values
    ):
        alerts = [_alert("A")]
        assert exclude_absent_value_alerts(alerts, assessment, values) == alerts

    def test_no_alerts_reads_as_an_empty_list(self):
        assert exclude_absent_value_alerts(None, {"A": {"confidence": 0}}, {}) == []

    def test_paths_are_spelled_as_the_alert_builder_spells_them(self):
        """The filter matches by path, so the walk must name leaves exactly as
        ``enrich_assessment_with_thresholds`` names its alerts — taken from the
        builder itself, not restated."""
        from idp_common.assessment.batching import enrich_assessment_with_thresholds

        schema = {
            "type": "object",
            "properties": {
                "A": {"type": "string"},
                "G": {"type": "object", "properties": {"B": {"type": "string"}}},
                "L": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"C": {"type": "string"}},
                    },
                },
            },
        }
        assessment = {
            "A": {"confidence": 0.1},
            "G": {"B": {"confidence": 0.1}},
            "L": [{"C": {"confidence": 0.1}}, {"C": {"confidence": 0.1}}],
        }
        _, alerts = enrich_assessment_with_thresholds(assessment, schema, 0.9)
        walked = [path for path, _l, _v in iter_assessed_leaf_values(assessment, {})]
        assert sorted(_names(alerts)) == sorted(walked)
        assert len(walked) == 4


# --------------------------------------------------------------------------- #
# Rule 2: missing required fields, JSON Schema ``required`` semantics
# --------------------------------------------------------------------------- #


def _obj(properties: dict, required: list | None = None, **extra) -> dict:
    schema: dict[str, Any] = {"type": "object", "properties": properties, **extra}
    if required is not None:
        schema["required"] = required
    return schema


S = {"type": "string"}


class TestCollectMissingRequiredFields:
    def test_a_schema_with_no_required_counts_nothing(self):
        schema = _obj(
            {
                "A": S,
                "G": _obj({"B": S}),
                "L": {"type": "array", "items": _obj({"C": S})},
            }
        )
        values = {"A": None, "G": None, "L": [{"C": None}]}
        assert collect_missing_required_fields(values, schema) == []

    def test_only_required_top_level_fields_are_counted(self):
        schema = _obj({"A": S, "B": S, "C": S}, required=["A", "C"])
        values = {"A": None, "B": None, "C": "present"}
        assert _names(collect_missing_required_fields(values, schema)) == ["A"]

    def test_a_required_name_without_a_property_is_still_required(self):
        schema = _obj({"A": S}, required=["Ghost"])
        assert _names(collect_missing_required_fields({"A": "x"}, schema)) == ["Ghost"]

    def test_a_nested_objects_required_applies_when_it_is_present(self):
        schema = _obj({"Addr": _obj({"City": S, "Zip": S}, required=["Zip"])})
        values = {"Addr": {"City": "Seattle", "Zip": ""}}
        assert _names(collect_missing_required_fields(values, schema)) == ["Addr.Zip"]

    def test_an_absent_non_required_parent_contributes_nothing(self):
        schema = _obj({"Addr": _obj({"City": S, "Zip": S}, required=["City", "Zip"])})
        assert collect_missing_required_fields({"Addr": None}, schema) == []

    @pytest.mark.parametrize("absent", [None, {}, {"City": None, "Zip": ""}])
    def test_an_absent_required_parent_is_one_entry_not_one_per_child(self, absent):
        schema = _obj(
            {"Addr": _obj({"City": S, "Zip": S}, required=["City", "Zip"])},
            required=["Addr"],
        )
        assert _names(collect_missing_required_fields({"Addr": absent}, schema)) == [
            "Addr"
        ]

    def test_array_item_required_applies_per_row_that_exists(self):
        schema = _obj(
            {
                "Rows": {
                    "type": "array",
                    "items": _obj({"Amount": S, "Memo": S}, required=["Amount"]),
                }
            }
        )
        values = {
            "Rows": [
                {"Amount": "1", "Memo": None},
                {"Amount": None, "Memo": "x"},
                None,
                {},
            ]
        }
        assert _names(collect_missing_required_fields(values, schema)) == [
            "Rows[1].Amount",
            "Rows[2].Amount",
            "Rows[3].Amount",
        ]

    def test_rows_exist_even_when_every_row_is_empty(self):
        """Whether a row's required fields count must not depend on whether a
        sibling row happens to hold a value."""
        schema = _obj(
            {"Rows": {"type": "array", "items": _obj({"A": S}, required=["A"])}}
        )
        found = collect_missing_required_fields({"Rows": [{"A": None}, None]}, schema)
        assert _names(found) == ["Rows[0].A", "Rows[1].A"]

    def test_a_present_non_required_object_of_nulls_is_absent(self):
        """Extraction emits an object it did not find as a skeleton of nulls;
        that object is absent, so its own ``required`` does not apply."""
        schema = _obj({"Addr": _obj({"City": S}, required=["City"])})
        assert collect_missing_required_fields({"Addr": {"City": None}}, schema) == []

    @pytest.mark.parametrize("required", [True, False])
    def test_an_empty_list_has_no_rows_and_counts_only_if_required(self, required):
        schema = _obj(
            {"Rows": {"type": "array", "items": _obj({"A": S}, required=["A"])}},
            required=["Rows"] if required else None,
        )
        found = _names(collect_missing_required_fields({"Rows": []}, schema))
        assert found == (["Rows"] if required else [])

    def test_ref_is_resolved_against_defs(self):
        schema = {
            "type": "object",
            "$defs": {"Row": _obj({"Amount": S, "Note": S}, required=["Amount"])},
            "properties": {
                "Rows": {"type": "array", "items": {"$ref": "#/$defs/Row"}},
                "Head": {"$ref": "#/$defs/Row"},
            },
        }
        values = {"Rows": [{"Amount": None}], "Head": {"Amount": "", "Note": "x"}}
        assert sorted(_names(collect_missing_required_fields(values, schema))) == [
            "Head.Amount",
            "Rows[0].Amount",
        ]

    def test_a_property_that_refs_an_array_definition_is_walked(self):
        schema = {
            "type": "object",
            "$defs": {
                "Rows": {
                    "type": "array",
                    "items": _obj({"Amount": S}, required=["Amount"]),
                }
            },
            "properties": {"Rows": {"$ref": "#/$defs/Rows"}},
        }
        found = collect_missing_required_fields({"Rows": [{"Amount": ""}]}, schema)
        assert _names(found) == ["Rows[0].Amount"]

    def test_a_multi_instance_wrapper_does_not_count_its_own_instances(self):
        """The wrapper's ``required: ["instances"]`` is the transform's, not the
        author's; the record's own ``required`` applies per instance row."""
        from idp_common.schema.multi_instance import wrap_class_schema

        def _class(required):
            return wrap_class_schema(
                _obj(
                    {"Name": S, "Id": S},
                    required=required,
                    **{"$id": "W2", "x-aws-idp-multi-instance": True},
                )
            )

        no_required = _class(None)
        assert no_required.get("required") == ["instances"]
        assert collect_missing_required_fields({"instances": []}, no_required) == []

        with_required = _class(["Id"])
        values = {"instances": [{"Name": "a", "Id": None}, {"Name": "b", "Id": "7"}]}
        assert _names(collect_missing_required_fields(values, with_required)) == [
            "instances[0].Id"
        ]

    def test_entries_carry_the_fields_score_when_it_has_one(self):
        schema = _obj(
            {"A": S, "G": _obj({"B": S})},
            required=["A", "G"],
        )
        assessment = {
            "A": {"confidence": 0.0, "confidence_threshold": 0.9},
            "G": {"B": {"confidence": 0.4}},
        }
        found = collect_missing_required_fields(
            {"A": None, "G": None}, schema, assessment
        )
        assert found == [
            {"attribute_name": "A", "confidence": 0.0, "confidence_threshold": 0.9},
            {"attribute_name": "G", "confidence": None, "confidence_threshold": None},
        ]

    @pytest.mark.parametrize("values", [None, [], "x"])
    def test_unusable_values_yield_nothing(self, values):
        assert collect_missing_required_fields(values, _obj({"A": S}, ["A"])) == []


# --------------------------------------------------------------------------- #
# Chokepoint 1: the standalone (``separate``) Assessment step
# --------------------------------------------------------------------------- #


_SEPARATE_CONFIG = {
    "classes": [
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "invoice",
            "x-aws-idp-document-type": "invoice",
            "type": "object",
            "required": ["invoice_number", "total_amount"],
            "properties": {
                "invoice_number": {"type": "string"},
                "invoice_date": {"type": "string"},
                "total_amount": {
                    "type": "string",
                    "x-aws-idp-confidence-threshold": 0.9,
                },
            },
        }
    ],
    "extraction": {"geometry": {"mode": "off"}},
    "assessment": {
        "model": "anthropic.claude-3-sonnet-20240229-v1:0",
        "system_prompt": "You are a document assessment assistant.",
        "task_prompt": (
            "Assess {DOCUMENT_CLASS}: {ATTRIBUTE_NAMES_AND_DESCRIPTIONS} "
            "{EXTRACTION_RESULTS} {DOCUMENT_TEXT}"
        ),
    },
}


def _run_separate_step(model_scores: dict[str, float]):
    from idp_common.assessment.service import AssessmentService

    doc = Document(
        id="d",
        input_key="d.pdf",
        input_bucket="in",
        output_bucket="out",
        status=Status.ASSESSING,
    )
    doc.pages["1"] = Page(
        page_id="1",
        image_uri="s3://in/d.pdf/pages/1/image.jpg",
        parsed_text_uri="s3://in/d.pdf/pages/1/parsed.txt",
    )
    doc.sections.append(
        Section(
            section_id="1",
            classification="invoice",
            page_ids=["1"],
            extraction_result_uri="s3://out/d.pdf/sections/1/result.json",
        )
    )
    extraction = {
        "document_class": {"type": "invoice"},
        "inference_result": {
            "invoice_number": "INV-1",
            "invoice_date": None,
            "total_amount": "",
        },
        "metadata": {"parsing_succeeded": True},
    }
    text = json.dumps(
        {
            k: {"confidence": v, "confidence_reason": "r"}
            for k, v in model_scores.items()
        }
    )
    response = {
        "response": {
            "stopReason": "end_turn",
            "output": {"message": {"content": [{"text": text}]}},
        },
        "metering": {},
    }
    svc = AssessmentService(region="us-west-2", config=_SEPARATE_CONFIG)
    with (
        patch("idp_common.s3.get_json_content", return_value=extraction),
        patch("idp_common.s3.get_text_content", return_value="Page 1 text"),
        patch("idp_common.image.prepare_image", return_value=b"img"),
        patch("idp_common.bedrock.invoke_model", return_value=response),
        patch("idp_common.s3.write_content") as write,
        patch("idp_common.metrics.put_metric"),
    ):
        result = svc.process_document_section(doc, "1")
    return result, write.call_args.args[0]


class TestSeparateAssessmentStep:
    def test_absent_fields_raise_no_alert_and_required_ones_are_listed(self):
        result, written = _run_separate_step(
            {"invoice_number": 0.5, "invoice_date": 0.0, "total_amount": 0.0}
        )
        section = result.sections[0]

        # Only the populated field alerts; both absent ones scored 0.0.
        assert _names(section.confidence_threshold_alerts) == ["invoice_number"]
        # total_amount is required and absent; invoice_date is absent but not
        # required, so it is not counted at all.
        assert section.missing_required_fields == [
            {
                "attribute_name": "total_amount",
                "confidence": 0.0,
                "confidence_threshold": 0.9,
            }
        ]
        # The score and its reason stay in explainability_info.
        explain = written["explainability_info"][0]
        assert explain["invoice_date"]["confidence"] == 0.0
        assert explain["total_amount"]["confidence_reason"] == "r"


# --------------------------------------------------------------------------- #
# Chokepoint 2: integrated confidence, in extraction
# --------------------------------------------------------------------------- #


_INTEGRATED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "$id": "Invoice",
    "required": ["Total"],
    "properties": {"Total": {"type": "string"}, "Vendor": {"type": "string"}},
}


def _extraction_service():
    from idp_common.extraction.service import ExtractionService

    svc = ExtractionService(
        region="us-west-2",
        config={
            "extraction": {
                "model": "us.amazon.nova-pro-v1:0",
                "geometry": {"mode": "off"},
            },
            "classes": [_INTEGRATED_SCHEMA],
        },
    )
    svc._class_schema = _INTEGRATED_SCHEMA
    svc._integrated_assessment_enabled = lambda: False  # type: ignore[method-assign]
    return svc


def _section_info():
    from idp_common.extraction.service import SectionInfo

    return SectionInfo(
        class_label="Invoice",
        sorted_page_ids=["1"],
        page_indices=[0],
        output_bucket="out",
        output_key="d.pdf/sections/1/result.json",
        output_uri="s3://out/d.pdf/sections/1/result.json",
        start_page=1,
        end_page=1,
    )


class TestIntegratedPath:
    def test_the_stored_alerts_skip_absent_values(self):
        svc = _extraction_service()
        section = Section(section_id="1", classification="Invoice", page_ids=["1"])
        svc._attach_explainability(
            output_metadata={},
            merged_assessment={
                "Total": {"confidence": 0.0},
                "Vendor": {"confidence": 0.2},
            },
            merged_assessment_alerts=[_alert("Total"), _alert("Vendor", 0.2)],
            extracted_fields={"Total": None, "Vendor": "Acme"},
            document=Document(id="d", input_key="d.pdf"),
            section=section,
            section_info=_section_info(),
        )
        assert _names(section.confidence_threshold_alerts) == ["Vendor"]

    def test_save_results_lists_missing_required_fields_with_their_score(self):
        from idp_common.extraction.service import ExtractionResult

        svc = _extraction_service()
        doc = Document(
            id="d",
            input_key="d.pdf",
            input_bucket="in",
            output_bucket="out",
            status=Status.EXTRACTING,
        )
        section = Section(section_id="1", classification="Invoice", page_ids=["1"])
        doc.sections = [section]
        result = ExtractionResult(
            extracted_fields={"Total": "", "Vendor": None},
            metering={
                "_merged_assessment": {
                    "Total": {"confidence": 0.0, "confidence_threshold": 0.8},
                    "Vendor": {"confidence": 0.0, "confidence_threshold": 0.8},
                },
                "_merged_assessment_alerts": [_alert("Total"), _alert("Vendor")],
            },
            parsing_succeeded=True,
            total_duration=1.0,
        )
        svc.config.extraction.agentic.enabled = True
        with patch("idp_common.extraction.service.s3.write_content"):
            svc._save_results(doc, section, result, _section_info(), "1", time.time())

        assert section.confidence_threshold_alerts == []
        assert section.missing_required_fields == [
            {"attribute_name": "Total", "confidence": 0.0, "confidence_threshold": 0.8}
        ]

    def test_save_results_lists_them_with_confidence_off_too(self):
        """A property of the values and the schema, so it needs no scores."""
        from idp_common.extraction.service import ExtractionResult

        svc = _extraction_service()
        doc = Document(id="d", input_key="d.pdf", status=Status.EXTRACTING)
        section = Section(section_id="1", classification="Invoice", page_ids=["1"])
        doc.sections = [section]
        result = ExtractionResult(
            extracted_fields={"Vendor": "Acme"},
            metering={},
            parsing_succeeded=True,
            total_duration=1.0,
        )
        with patch("idp_common.extraction.service.s3.write_content"):
            svc._save_results(doc, section, result, _section_info(), "1", time.time())

        assert section.missing_required_fields == [
            {
                "attribute_name": "Total",
                "confidence": None,
                "confidence_threshold": None,
            }
        ]


# --------------------------------------------------------------------------- #
# Chokepoint 3: BDA mode
# --------------------------------------------------------------------------- #


class TestBdaMode:
    def _details(self, value: Any) -> dict:
        return {
            "key_value_details": {
                "1": [
                    {
                        "key": "InvoiceTotal",
                        "value": value,
                        "confidence": 0.1,
                        "confidence_threshold": 0.8,
                    }
                ]
            }
        }

    @pytest.mark.parametrize("value", [None, "", "  "])
    def test_an_entry_with_no_value_raises_no_alert(self, value):
        from tests.unit.assessment.test_bda_lambda_functions import bda_index

        assert (
            bda_index.create_confidence_threshold_alerts(self._details(value), 0.8)
            == []
        )

    def test_an_entry_with_a_value_still_alerts(self):
        from tests.unit.assessment.test_bda_lambda_functions import bda_index

        assert _names(
            bda_index.create_confidence_threshold_alerts(self._details("12.00"), 0.8)
        ) == ["InvoiceTotal"]

    def test_process_segments_lists_missing_required_fields(self):
        from tests.unit.assessment.test_bda_confidence_parsing import _run

        body = json.dumps(
            {
                "matched_blueprint": {"name": "invoice-bp", "confidence": 0.99},
                "split_document": {"page_indices": [0]},
                "document_class": {"type": "invoice"},
                "inference_result": {"InvoiceTotal": "", "Vendor": "Acme"},
                "explainability_info": [
                    {
                        "InvoiceTotal": {
                            "value": "",
                            "confidence": 0.0,
                            "geometry": [{"page": 1}],
                        },
                        "Vendor": {
                            "value": "Acme",
                            "confidence": 0.95,
                            "geometry": [{"page": 1}],
                        },
                    }
                ],
            }
        ).encode()
        schema = {
            "type": "object",
            "required": ["InvoiceTotal"],
            "properties": {"InvoiceTotal": S, "Vendor": S},
        }
        result, hitl = _run([body], schema)
        section = result.sections[0]
        assert section.confidence_threshold_alerts == []
        assert _names(section.missing_required_fields) == ["InvoiceTotal"]
        assert section.missing_required_fields[0]["confidence"] == 0.0
        # The absent field no longer sends the section to legacy review.
        assert hitl is False


# --------------------------------------------------------------------------- #
# The document count and its persistence
# --------------------------------------------------------------------------- #


class TestDocumentCount:
    def test_the_count_round_trips_through_the_document_model(self):
        doc = Document(id="d", input_key="d.pdf")
        doc.sections = [
            Section(
                section_id="1",
                classification="c",
                missing_required_fields=[_alert("A", 0.0)],
            )
        ]
        doc.missing_required_field_count = 1
        back = Document.from_dict(doc.to_dict())
        assert back.missing_required_field_count == 1
        assert back.sections[0].missing_required_fields == [_alert("A", 0.0)]

    def test_a_section_round_trips_its_list(self):
        section = Section(
            section_id="1", classification="c", missing_required_fields=[_alert("A")]
        )
        back = Section.from_dict(section.to_dict())
        assert back.missing_required_fields == [_alert("A")]
        assert (
            "missing_required_fields"
            not in Section(section_id="2", classification="c").to_dict()
        )

    def test_a_zero_count_is_omitted_from_the_payload(self):
        payload = Document(id="d", input_key="d.pdf").to_dict()
        assert "missing_required_field_count" not in payload
        assert Document.from_dict(payload).missing_required_field_count == 0

    def test_the_tracking_item_stores_list_and_count_and_reads_them_back(self):
        from idp_common.dynamodb.service import DocumentDynamoDBService

        svc = DocumentDynamoDBService.__new__(DocumentDynamoDBService)
        doc = Document(id="d", input_key="d.pdf", status=Status.COMPLETED)
        doc.sections = [
            Section(
                section_id="1",
                classification="c",
                page_ids=["1"],
                missing_required_fields=[_alert("A", 0.0, 0.9)],
            )
        ]
        doc.missing_required_field_count = 1
        expression, names, values = svc._document_to_update_expressions(doc)
        assert "#MissingRequiredFieldCount = :MissingRequiredFieldCount" in expression
        assert values[":MissingRequiredFieldCount"] == 1
        stored = values[":Sections"][0]["MissingRequiredFields"]
        assert stored[0]["attributeName"] == "A"

        item = {
            "PK": "doc#d.pdf",
            "ObjectKey": "d.pdf",
            "ObjectStatus": "COMPLETED",
            "MissingRequiredFieldCount": 1,
            "Sections": values[":Sections"],
        }
        back = svc._dynamodb_item_to_document(item)
        assert back.missing_required_field_count == 1
        assert _names(back.sections[0].missing_required_fields) == ["A"]

    def test_a_bare_document_does_not_overwrite_the_stored_count(self):
        """A document object with no sections knows nothing about the count."""
        from idp_common.dynamodb.service import DocumentDynamoDBService

        svc = DocumentDynamoDBService.__new__(DocumentDynamoDBService)
        expression, _names_, _values = svc._document_to_update_expressions(
            Document(id="d", input_key="d.pdf", status=Status.FAILED)
        )
        assert "MissingRequiredFieldCount" not in expression

    def test_the_stored_list_is_capped_and_the_true_total_recorded(self):
        from idp_common.dynamodb.service import (
            _MAX_STORED_MISSING_REQUIRED_PER_SECTION,
            set_section_missing_required_fields,
        )

        cap = _MAX_STORED_MISSING_REQUIRED_PER_SECTION
        section = Section(
            section_id="1",
            classification="c",
            missing_required_fields=[_alert(f"Rows[{i}].A") for i in range(cap + 7)],
        )
        data: dict[str, Any] = {}
        set_section_missing_required_fields(data, section)
        assert len(data["MissingRequiredFields"]) == cap
        assert data["MissingRequiredFieldsTotal"] == cap + 7


class TestProcessResultsCount:
    """The Pipeline-mode result step sums the sections into the document count,
    and a section with only missing required fields is not sent to review."""

    def test_the_document_count_is_the_sum_over_sections(self, monkeypatch):
        from unittest.mock import MagicMock

        from tests.unit.lambdas.test_processresults_feature_off_parity import (
            _base_document,
            _config,
            processresults_index,
        )

        def _result(section_id: str, missing_names: list[str]) -> dict:
            doc = Document(
                id="folder/inv.pdf",
                input_bucket="in",
                input_key="folder/inv.pdf",
                output_bucket="out",
                status=Status.EXTRACTING,
                sections=[
                    Section(
                        section_id=section_id,
                        classification="invoice",
                        page_ids=[section_id],
                        missing_required_fields=[_alert(n) for n in missing_names],
                    )
                ],
            )
            return {"document": doc.to_dict()}

        doc_service = MagicMock()
        doc_service.get_document.return_value = None
        monkeypatch.setenv("WORKING_BUCKET", "")
        monkeypatch.setattr(
            processresults_index, "get_config", lambda **kw: _config(True)
        )
        monkeypatch.setattr(
            processresults_index,
            "create_document_service",
            lambda *a, **kw: doc_service,
        )
        monkeypatch.setattr(
            processresults_index, "create_metadata_file", lambda *a, **kw: None
        )
        processresults_index.handler(
            {
                "ClassificationResult": {"document": _base_document().to_dict()},
                "ExtractionResults": [_result("1", ["A", "B"]), _result("2", ["C"])],
                "execution_arn": "arn:aws:states:us-east-1:111122223333:execution:sm:e",
            },
            None,
        )
        written = doc_service.update_document.call_args.args[0]
        assert written.missing_required_field_count == 3
        assert written.confidence_alert_count == 0
        assert not written.hitl_triggered
