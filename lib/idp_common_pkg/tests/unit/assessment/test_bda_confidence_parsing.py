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
        patch.object(bda_index, "resolve_class_schema", return_value=class_schema),
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
