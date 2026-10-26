# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Whether a BDA segment goes to review, by blueprint confidence, in both modes.

Both review modes read a matched blueprint's confidence through one reader,
``parse_blueprint_confidence`` (GitHub #1581). An unscored blueprint confidence
(null, non-numeric, non-finite, a bool, out of range, or a percentage string) is
held for review in both: legacy HITL marks the segment, and review-queue mode adds
a ``blueprint_confidence`` reason. The change was made under one constraint, that
no input is reviewed in fewer cells than before, and ``BEFORE`` below is what
makes that checkable.

``BEFORE`` was measured, not written: it is the output of :func:`_decide` over
this same grid, run against the unmodified code at ``backlog/staging``
``ba1025149``. Every fixture is a custom-output result's raw bytes, read by the
Lambda's own ``download_json`` (``json.loads``), so the value each mode sees is
the one ``json.loads`` produces (``1e400`` written as an integer literal stays an
``int``; ``NaN`` and ``Infinity`` are accepted).
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from idp_common.review_queues.reasons import build_reasons
from idp_common.utils import parse_blueprint_confidence, parse_confidence
from tests.unit.assessment.test_bda_lambda_functions import bda_index

pytestmark = pytest.mark.unit

#: Blueprint confidence literals as they appear in the result JSON.
LITERALS = [
    '"95%"',
    '"50%"',
    '"0.9%"',
    "true",
    "95",
    "50",
    "1.5",
    "150",
    "0.95",
    "0.5",
    "1",
    "0",
    "false",
    "-0.2",
    '"0.95"',
    '"high"',
    "null",
    "NaN",
    "Infinity",
    "1" + "0" * 400,
    # Found by review: a negative string, and a string carrying a character
    # str.strip removes but float() rejects (JSON \u001c is "\x1c").
    '"-0.2"',
    '"0.5\\u001c"',
]
#: 0.0 is a valid ``hitl.confidence_threshold`` (``ge=0.0``).
THRESHOLDS = [0.0, 0.5, 0.8, 0.99]
MODES = ["legacy", "queue"]


def _body(bp_literal: str) -> bytes:
    # The field's confidence is 1.0, so no field alert is raised at any
    # threshold and the decision is the blueprint confidence's alone.
    return (
        "{"
        f'"matched_blueprint": {{"name": "invoice-bp", "confidence": {bp_literal}}},'
        '"split_document": {"page_indices": [0]},'
        '"document_class": {"type": "invoice"},'
        '"explainability_info": [{"InvoiceTotal": {"value": "12.00",'
        ' "confidence": 1.0, "geometry": [{"page": 1}]}}]'
        "}"
    ).encode()


def _decide(bp_literal: str, threshold: float, mode: str) -> bool:
    """True when the segment is reviewed: legacy HITL marks it, or queue mode
    builds a reason for it (``route_for_review`` holds exactly when it does)."""
    from idp_common.models import Document, Section

    body = _body(bp_literal)

    def get_object(Bucket, Key):  # noqa: N803 - boto3 keyword names
        assert Key == "out/1.json"
        return {"Body": io.BytesIO(body)}

    doc = Document(id="pkg.pdf", input_key="pkg.pdf")
    doc.sections = [Section(section_id="1", classification="invoice", page_ids=["1"])]
    config = SimpleNamespace(
        hitl=SimpleNamespace(enabled=True, confidence_threshold=threshold),
        classes=[],
    )
    with (
        patch.object(bda_index.s3_client, "get_object", side_effect=get_object),
        patch.object(bda_index, "legacy_hitl_enabled", return_value=(mode == "legacy")),
        patch.object(bda_index, "resolve_class_schema", return_value=None),
        patch.dict("os.environ", {"DB_NAME": ""}, clear=False),
        patch.object(bda_index, "boto3"),
        patch.object(bda_index, "metrics"),
    ):
        result, hitl = bda_index.process_segments(
            input_bucket="in",
            output_bucket="out",
            object_key="pkg.pdf",
            segment_metadata=[
                {
                    "custom_output_status": "MATCH",
                    "custom_output_path": "s3://bda/out/1.json",
                }
            ],
            confidence_threshold=threshold,
            execution_id="exec-1",
            document=doc,
            config=config,
        )
    # No field alert in any cell, so only the blueprint confidence decides.
    assert result.sections[0].confidence_threshold_alerts == []
    if mode == "legacy":
        return bool(hitl)
    # In queue mode the legacy marking is off for every segment.
    assert hitl is False
    assert [m.hitl_triggered for m in result.hitl_metadata] == [None]
    return bool(build_reasons(result, config, mode="bda"))


CELLS = [(lit, t, m) for lit in LITERALS for t in THRESHOLDS for m in MODES]

#: Every positive threshold in the grid.
_ALL = {0.5, 0.8, 0.99}
#: Measured: the thresholds at which each input was reviewed, per mode, before
#: #1581 (``_decide`` run against ``backlog/staging`` ``ba1025149``).
BEFORE: dict[str, dict[str, set[float]]] = {
    '"95%"': {"legacy": _ALL, "queue": {0.99}},
    '"50%"': {"legacy": _ALL, "queue": {0.8, 0.99}},
    '"0.9%"': {"legacy": _ALL, "queue": {0.99}},
    "true": {"legacy": set(), "queue": set()},
    "95": {"legacy": set(), "queue": {0.99}},
    "50": {"legacy": set(), "queue": {0.8, 0.99}},
    "1.5": {"legacy": set(), "queue": _ALL},
    "150": {"legacy": set(), "queue": set()},
    "0.95": {"legacy": {0.99}, "queue": {0.99}},
    "0.5": {"legacy": {0.8, 0.99}, "queue": {0.8, 0.99}},
    "1": {"legacy": set(), "queue": set()},
    "0": {"legacy": _ALL, "queue": _ALL},
    "false": {"legacy": _ALL, "queue": set()},
    "-0.2": {"legacy": _ALL | {0.0}, "queue": set()},
    '"0.95"': {"legacy": {0.99}, "queue": {0.99}},
    '"high"': {"legacy": _ALL, "queue": set()},
    "null": {"legacy": _ALL, "queue": set()},
    "NaN": {"legacy": _ALL, "queue": set()},
    "Infinity": {"legacy": _ALL, "queue": set()},
    LITERALS[-3]: {"legacy": _ALL, "queue": set()},
    '"-0.2"': {"legacy": _ALL | {0.0}, "queue": set()},
    LITERALS[-1]: {"legacy": _ALL, "queue": {0.8, 0.99}},
}

#: The specification, written from the decision rather than read off the code:
#: what the shared reader makes of each input. ``None`` is unscored, which is
#: reviewed at every threshold, 0.0 included.
SPEC_READING: dict[str, float | None] = {
    '"95%"': None,  # a percentage string is unscored
    '"50%"': None,
    '"0.9%"': None,
    "true": None,  # a bool is not a confidence
    "95": 0.95,  # a bare number in (1, 100] is a percentage
    "50": 0.5,
    "1.5": 0.015,
    "150": None,  # out of range
    "0.95": 0.95,
    "0.5": 0.5,
    "1": 1.0,
    "0": 0.0,
    "false": None,
    "-0.2": None,
    '"0.95"': 0.95,
    '"high"': None,
    "null": None,
    "NaN": None,
    "Infinity": None,
    LITERALS[-3]: None,  # too large for a float
    '"-0.2"': None,
    LITERALS[-1]: None,  # float() rejects it, although str.strip would not
}


def test_the_tables_cover_the_grid():
    assert list(BEFORE) == LITERALS
    assert list(SPEC_READING) == LITERALS
    assert len(CELLS) == len(LITERALS) * len(THRESHOLDS) * len(MODES) == 176


@pytest.mark.parametrize("literal,threshold,mode", CELLS)
def test_no_input_is_reviewed_less_than_before(literal, threshold, mode):
    before = threshold in BEFORE[literal][mode]
    after = _decide(literal, threshold, mode)
    assert after >= before


@pytest.mark.parametrize("literal,threshold,mode", CELLS)
def test_each_cell_follows_the_shared_reading(literal, threshold, mode):
    reading = SPEC_READING[literal]
    expected = reading is None or reading < threshold
    assert _decide(literal, threshold, mode) is expected


@pytest.mark.parametrize("literal", LITERALS)
@pytest.mark.parametrize("threshold", THRESHOLDS)
def test_the_two_modes_agree(literal, threshold):
    assert _decide(literal, threshold, "legacy") is _decide(literal, threshold, "queue")


@pytest.mark.parametrize("literal", LITERALS)
def test_the_section_records_the_shared_reading(literal):
    """The section's class confidence is the reader's value, and an unscored
    match is flagged, which is what queue routing reads."""
    from idp_common.models import Document, Section

    body = _body(literal)
    doc = Document(id="pkg.pdf", input_key="pkg.pdf")
    doc.sections = [Section(section_id="1", classification="invoice", page_ids=["1"])]
    with (
        patch.object(
            bda_index.s3_client,
            "get_object",
            side_effect=lambda Bucket, Key: {"Body": io.BytesIO(body)},  # noqa: N803
        ),
        patch.object(bda_index, "legacy_hitl_enabled", return_value=False),
        patch.object(bda_index, "resolve_class_schema", return_value=None),
        patch.dict("os.environ", {"DB_NAME": ""}, clear=False),
        patch.object(bda_index, "boto3"),
        patch.object(bda_index, "metrics"),
    ):
        result, _ = bda_index.process_segments(
            input_bucket="in",
            output_bucket="out",
            object_key="pkg.pdf",
            segment_metadata=[
                {"custom_output_status": "MATCH", "custom_output_path": "s3://b/k"}
            ],
            confidence_threshold=0.8,
            execution_id="exec-1",
            document=doc,
        )
    [section] = result.sections
    expected = SPEC_READING[literal]
    if expected is None:
        assert section.confidence is None
    else:
        assert section.confidence == pytest.approx(expected)
    assert section.blueprint_unscored is (expected is None)


def test_an_unmatched_segment_leaves_its_section_unflagged():
    """A section whose segment matched no blueprint is unscored too, but it is
    not an unscored match: legacy HITL does not mark it, so queue mode must not
    hold it either."""
    from idp_common.models import Document, Section

    std = b'{"metadata": {"start_page_index": 0, "end_page_index": 0}}'
    doc = Document(id="pkg.pdf", input_key="pkg.pdf")
    doc.sections = [Section(section_id="1", classification="invoice", page_ids=["1"])]
    config = SimpleNamespace(
        hitl=SimpleNamespace(enabled=True, confidence_threshold=0.8), classes=[]
    )
    with (
        patch.object(
            bda_index.s3_client,
            "get_object",
            side_effect=lambda Bucket, Key: {"Body": io.BytesIO(std)},  # noqa: N803
        ),
        patch.object(bda_index, "legacy_hitl_enabled", return_value=False),
        patch.dict("os.environ", {"DB_NAME": ""}, clear=False),
        patch.object(bda_index, "boto3"),
        patch.object(bda_index, "metrics"),
    ):
        result, _ = bda_index.process_segments(
            input_bucket="in",
            output_bucket="out",
            object_key="pkg.pdf",
            segment_metadata=[
                {"custom_output_status": "NO_MATCH", "standard_output_path": "s3://b/k"}
            ],
            confidence_threshold=0.8,
            execution_id="exec-1",
            document=doc,
            config=config,
        )
    [section] = result.sections
    assert section.confidence is None
    assert section.blueprint_unscored is False
    assert build_reasons(result, config, mode="bda") == []


class TestParseBlueprintConfidence:
    @pytest.mark.parametrize(
        "value",
        ["95%", " 50 %", "95% ", "95%%", "0.9%", "100%", "0%", "%", "0.5\x1c", "-0.2"],
    )
    def test_a_percentage_string_is_unscored(self, value):
        assert parse_blueprint_confidence(value) is None

    @pytest.mark.parametrize(
        "value,expected",
        [(0.95, 0.95), ("0.95", 0.95), (95, 0.95), (1.5, 0.015), (1, 1.0), (0, 0.0)],
    )
    def test_anything_else_reads_as_parse_confidence_does(self, value, expected):
        assert parse_blueprint_confidence(value) == pytest.approx(expected)
        assert parse_confidence(value) == pytest.approx(expected)

    @pytest.mark.parametrize(
        "value", [None, True, False, "high", "", -0.2, 150, float("nan")]
    )
    def test_unusable_values_are_unscored(self, value):
        assert parse_blueprint_confidence(value) is None

    def test_parse_confidence_still_reads_a_percentage_string(self):
        """Classification confidences keep their percentage reading."""
        assert parse_confidence("95%") == pytest.approx(0.95)
        assert parse_confidence("0.9%") == pytest.approx(0.009)


class TestBlueprintUnscoredPersists:
    """The flag travels with the document's own serialization (the workflow
    payload between steps). The DynamoDB tracking record does not carry it, so a
    reprocess that reloads the document from DynamoDB starts unflagged; that is
    no fewer reviews than before, when queue mode never held an unscored match."""

    def test_section_round_trip(self):
        from idp_common.models import Section

        flagged = Section(section_id="1", classification="x", blueprint_unscored=True)
        assert flagged.to_dict()["blueprint_unscored"] is True
        assert Section.from_dict(flagged.to_dict()).blueprint_unscored is True
        plain = Section(section_id="1", classification="x")
        assert "blueprint_unscored" not in plain.to_dict()
        assert Section.from_dict(plain.to_dict()).blueprint_unscored is False

    def test_document_round_trip(self):
        from idp_common.models import Document, Section

        doc = Document(id="d", input_key="d")
        doc.sections = [
            Section(section_id="1", classification="x", blueprint_unscored=True),
            Section(section_id="2", classification="x"),
        ]
        data = doc.to_dict()
        assert [("blueprint_unscored" in s) for s in data["sections"]] == [True, False]
        back = Document.from_dict(data)
        assert [s.blueprint_unscored for s in back.sections] == [True, False]
