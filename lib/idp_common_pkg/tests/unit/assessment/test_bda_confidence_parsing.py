# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""BDA result values that used to escape ``process_segments``, or suppress an
alert, read through the Lambda's own result reader.

Each fixture is the raw bytes of a custom-output result, parsed by the Lambda's
``download_json``/``download_decimal`` (``json.loads``) rather than written into
a dict, so the tests also pin that the value survives the reader: ``json.loads``
keeps a long integer literal as an ``int``, accepts ``NaN`` and ``Infinity``,
and returns whatever top-level JSON value the object holds.
"""

from __future__ import annotations

import io
import json
from typing import Any, Optional
from unittest.mock import patch

import pytest

from idp_common.utils import parse_confidence
from tests.unit.assessment.test_bda_lambda_functions import bda_index

pytestmark = pytest.mark.unit

#: An integer literal too large for a float: ``float()`` raises OverflowError.
HUGE_INT = "1" + "0" * 400


def _body(
    page_index: int,
    *,
    field_conf: str = "0.95",
    bp_conf: str = "0.99",
) -> bytes:
    return (
        "{"
        f'"matched_blueprint": {{"name": "invoice-bp", "confidence": {bp_conf}}},'
        f'"split_document": {{"page_indices": [{page_index}]}},'
        '"document_class": {"type": "invoice"},'
        '"explainability_info": [{"InvoiceTotal": {"value": "12.00",'
        f' "confidence": {field_conf}, "geometry": [{{"page": {page_index + 1}}}]}}}}]'
        "}"
    ).encode()


def _run(bodies: list[bytes], class_schema: Optional[dict[str, Any]] = None):
    """``process_segments`` over one MATCH segment per body, legacy HITL on."""
    from idp_common.models import Document, Section

    keyed = {f"out/{n}.json": b for n, b in enumerate(bodies, start=1)}

    def get_object(Bucket, Key):  # noqa: N803 - boto3 keyword names
        return {"Body": io.BytesIO(keyed[Key])}

    doc = Document(id="pkg.pdf", input_key="pkg.pdf")
    doc.sections = [
        Section(section_id=str(n), classification="invoice", page_ids=[str(n)])
        for n in range(1, len(bodies) + 1)
    ]
    segments = [
        {"custom_output_status": "MATCH", "custom_output_path": f"s3://bda/{k}"}
        for k in keyed
    ]
    with (
        patch.object(bda_index.s3_client, "get_object", side_effect=get_object),
        patch.object(bda_index, "legacy_hitl_enabled", return_value=True),
        patch.object(
            bda_index,
            "resolve_class_schema",
            # Only the result's own class resolves, so a test using a schema
            # also pins that ``document_class.type`` reaches the resolver.
            side_effect=lambda doc_class, config: (
                class_schema if doc_class == "invoice" else None
            ),
        ),
        patch.dict("os.environ", {"DB_NAME": ""}, clear=False),
        patch.object(bda_index, "boto3"),
        patch.object(bda_index, "metrics"),
    ):
        return bda_index.process_segments(
            input_bucket="in",
            output_bucket="out",
            object_key="pkg.pdf",
            segment_metadata=segments,
            confidence_threshold=0.8,
            execution_id="exec-1",
            document=doc,
        )


class TestHugeIntegerBlueprintConfidence:
    def test_parse_confidence_reads_it_as_unscored(self):
        value = json.loads(f'{{"c": {HUGE_INT}}}')["c"]
        assert isinstance(value, int)
        assert parse_confidence(value) is None

    def test_every_segment_is_processed(self):
        """``parse_confidence`` raised OverflowError out of ``process_segments``,
        which the handler's catch-all swallowed, so every later segment lost its
        alerts, class confidence and HITL metadata."""
        result, hitl = _run([_body(0), _body(1, bp_conf=HUGE_INT), _body(2)])
        assert len(result.hitl_metadata) == 3
        assert [s.confidence for s in result.sections] == [0.99, None, 0.99]
        # Unscored, so legacy HITL marks it for review.
        assert [m.hitl_triggered for m in result.hitl_metadata] == [
            False,
            True,
            False,
        ]
        assert hitl is True


def _schema(threshold_literal: str) -> dict[str, Any]:
    """A class schema as the configuration reader would load it."""
    return json.loads(
        '{"type": "object", "properties": {"InvoiceTotal": {"type": "string",'
        f' "x-aws-idp-confidence-threshold": {threshold_literal}}}}}}}'
    )


class TestUnusableFieldThreshold:
    @pytest.mark.parametrize("literal", ["NaN", "-Infinity", '"NaN"', '"-inf"'])
    def test_a_nan_or_negative_infinite_schema_threshold_falls_back(self, literal):
        """The schema resolver passes these through, and ``0.1 < nan`` is False,
        so a low-confidence field raised no alert and skipped review. The
        default threshold applies instead."""
        result, hitl = _run([_body(0, field_conf="0.1")], _schema(literal))
        assert result.sections[0].confidence_threshold_alerts == [
            {
                "attribute_name": "InvoiceTotal",
                "confidence": 0.1,
                "confidence_threshold": 0.8,
            }
        ]
        assert hitl is True

    @pytest.mark.parametrize("literal", ["Infinity", '"inf"'])
    def test_a_positive_infinite_schema_threshold_still_alerts_every_field(
        self, literal
    ):
        """Unchanged: falling back would review less than before."""
        result, hitl = _run([_body(0, field_conf="0.95")], _schema(literal))
        assert result.sections[0].confidence_threshold_alerts == [
            {
                "attribute_name": "InvoiceTotal",
                "confidence": 0.95,
                "confidence_threshold": float("inf"),
            }
        ]
        assert hitl is True

    def test_a_finite_schema_threshold_is_still_used(self):
        result, _ = _run([_body(0, field_conf="0.85")], _schema("0.9"))
        assert result.sections[0].confidence_threshold_alerts == [
            {
                "attribute_name": "InvoiceTotal",
                "confidence": 0.85,
                "confidence_threshold": 0.9,
            }
        ]

    @pytest.mark.parametrize("threshold", ["high", "0.9x", [], {}, float("nan")])
    def test_an_unusable_entry_threshold_falls_back_without_raising(self, threshold):
        """The schema resolver already turns a non-numeric schema value into the
        default, so this is the builder's own guard: a non-numeric threshold
        raised TypeError when compared."""
        details = {
            "key_value_details": {
                "1": [
                    {"key": "A", "confidence": 0.5, "confidence_threshold": threshold}
                ]
            }
        }
        assert bda_index.create_confidence_threshold_alerts(details, 0.8) == [
            {"attribute_name": "A", "confidence": 0.5, "confidence_threshold": 0.8}
        ]


def _with(page_index: int, key: str, literal: str) -> bytes:
    """``_body(page_index)`` with the top-level ``key`` replaced by ``literal``."""
    output = json.loads(_body(page_index))
    output[key] = "__REPLACED__"
    return json.dumps(output).replace('"__REPLACED__"', literal).encode()


class TestUnusableCustomOutput:
    """An unusable custom-output result raised AttributeError or TypeError out of
    ``process_segments``, which the handler's catch-all swallowed, so every later
    segment lost its alerts, class confidence and HITL metadata."""

    @pytest.mark.parametrize("raw", [b"[]", b"null", b"42", b'"x"', b"true"])
    def test_a_result_that_is_not_an_object_is_an_unscored_match(self, raw):
        result, hitl = _run([_body(0), raw, _body(2)])
        assert len(result.hitl_metadata) == 3
        assert [m.extraction_bp_name for m in result.hitl_metadata] == [
            "invoice-bp",
            "None",
            "invoice-bp",
        ]
        assert result.sections[0].confidence == 0.99
        assert result.sections[2].confidence == 0.99
        # No blueprint confidence, so legacy HITL marks it for review.
        assert [m.hitl_triggered for m in result.hitl_metadata] == [
            False,
            True,
            False,
        ]
        assert hitl is True

    @pytest.mark.parametrize(
        ("key", "literal", "middle"),
        [
            # With no usable page index the segment matches no section, so the
            # middle section is left unscored.
            ("split_document", "null", None),
            ("split_document", '"pages"', None),
            ("split_document", '{"page_indices": null}', None),
            ("split_document", '{"page_indices": 5}', None),
            ("split_document", '{"page_indices": []}', None),
            ("split_document", '{"page_indices": ["a", null]}', None),
            # A bool is kept as an index, as before: ``true + 1`` is page 2.
            ("split_document", '{"page_indices": ["a", null, true]}', 0.99),
            ("explainability_info", "null", 0.99),
            ("explainability_info", "5", 0.99),
            ("document_class", '"invoice"', 0.99),
        ],
    )
    def test_a_malformed_member_does_not_stop_later_segments(
        self, key, literal, middle
    ):
        result, _ = _run([_body(0), _with(1, key, literal), _body(2)])
        assert len(result.hitl_metadata) == 3
        assert [s.confidence for s in result.sections] == [0.99, middle, 0.99]
        assert [m.hitl_triggered for m in result.hitl_metadata] == [
            False,
            False,
            False,
        ]


class TestKeyValuePagePlacement:
    def test_a_field_is_placed_on_its_own_page(self):
        explainability = json.loads(_body(1))["explainability_info"]
        details = bda_index.process_keyvalue_details(explainability, [1, 2], 0.8)
        assert [e["key"] for e in details["key_value_details"]["2"]] == ["InvoiceTotal"]
        assert details["key_value_details"]["1"] == []
        assert [e["key"] for e in details["bounding_box_details"]["2"]] == [
            "InvoiceTotal"
        ]
        assert details["bounding_box_details"]["1"] == []

    def test_with_no_page_indices_fields_land_on_page_zero(self):
        """Indexing the fallback page "0" raised KeyError before."""
        explainability = json.loads(_body(1))["explainability_info"]
        details = bda_index.process_keyvalue_details(explainability, [], 0.8)
        assert [e["key"] for e in details["key_value_details"]["0"]] == ["InvoiceTotal"]
        assert [e["key"] for e in details["bounding_box_details"]["0"]] == [
            "InvoiceTotal"
        ]
