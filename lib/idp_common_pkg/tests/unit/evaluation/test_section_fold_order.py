# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A document's evaluation does not depend on the order its sections finish (#2133).

``evaluate_document`` evaluates sections in parallel. Its weighted score is a float
sum over sections and its excluded-sections list is appended to section by
section, so folding them in completion order gave a different score in the last
digits, and a differently ordered list, from one evaluation of a document to the
next.
"""

import threading
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from idp_common.evaluation.models import SectionEvaluationResult
from idp_common.evaluation.service import EvaluationService
from idp_common.models import Document, Section

# Sections 1-3 are scored, 4-5 have no extractable schema. The three scores sum
# to 0.6000000000000001 in section order and to 0.6 in reverse.
SCORES = {"1": 0.1, "2": 0.2, "3": 0.3}
SECTION_IDS = ["1", "2", "3", "4", "5"]


def _document(bucket: str) -> Document:
    return Document(
        id="doc-a",
        input_key="doc-a",
        output_bucket=bucket,
        sections=[
            Section(
                section_id=sid,
                classification="Form",
                page_ids=[sid],
                extraction_result_uri=f"s3://{bucket}/doc-a/sections/{sid}/result.json",
            )
            for sid in SECTION_IDS
        ],
    )


@contextmanager
def _no_doc_split():
    """Stub the doc-split pass, including the S3 reads it makes for its sections."""
    with (
        patch("idp_common.evaluation.service.DocSplitClassificationMetrics") as split,
        patch(
            "idp_common.evaluation.service.load_sections_for_doc_split",
            return_value=[],
        ),
    ):
        split.return_value.calculate_all_metrics = MagicMock(return_value={})
        split.return_value.sections_pred = []
        yield


def _service(process, workers):
    service = EvaluationService.__new__(EvaluationService)
    service.max_workers = workers
    service._process_section = MagicMock(side_effect=process)  # type: ignore[method-assign]
    service.stickler_models = {}
    service._auto_generated_models = set()
    return service


def _evaluate(finish_order):
    """Evaluate the document with its sections finishing in ``finish_order``."""
    done = {sid: threading.Event() for sid in finish_order}
    previous = {sid: finish_order[n - 1] for n, sid in enumerate(finish_order) if n}

    def process(actual_section, _expected_section, _doc_ctx):
        sid = actual_section.section_id
        if sid in previous:
            done[previous[sid]].wait(timeout=5)
        if sid in SCORES:
            metrics = {
                "weighted_overall_score": SCORES[sid],
                "_stickler_counts": {"tp": 1, "fp": 0, "tn": 0, "fn": 0},
            }
            counts = {"tp": 1, "fp": 0, "fn": 0, "tn": 0, "fp1": 0, "fp2": 0}
        else:
            metrics = {"evaluation_skipped": True}
            counts = {}
        done[sid].set()
        return SectionEvaluationResult(sid, "Form", [], metrics), counts

    service = _service(process, workers=len(SECTION_IDS))

    with _no_doc_split():
        result = service.evaluate_document(
            _document("out"), _document("baseline"), store_results=False
        ).evaluation_result
    assert result is not None
    return (
        result.overall_metrics["weighted_overall_score"],
        [s["section_id"] for s in result.excluded_sections],
    )


@pytest.mark.unit
def test_weighted_score_and_excluded_sections_follow_section_order():
    forward = _evaluate(SECTION_IDS)
    backward = _evaluate(SECTION_IDS[::-1])
    in_section_order = (0.1 + 0.2 + 0.3) / 3
    assert forward == backward == (in_section_order, ["4", "5"])


@pytest.mark.unit
def test_a_transient_failure_stops_the_document_without_waiting_for_earlier_sections():
    """Folding in section order must not delay the retry. Section 1 is slow and
    succeeds, section 2 throttles at once: the transient failure has to cancel the
    sections still queued as soon as it happens, not once the fold reaches
    section 2 after section 1 finishes."""
    from botocore.exceptions import ClientError

    from idp_common.utils.transient_errors import TransientError

    throttle = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "m"}}, "Converse"
    )
    started = []

    def process(actual_section, _expected_section, _doc_ctx):
        sid = actual_section.section_id
        started.append(sid)
        if sid == "2":
            raise throttle
        # Long enough that, were the failure only seen after section 1, the two
        # workers would have taken sections 4 and 5 by then.
        time.sleep(0.6 if sid == "1" else 0.2)
        metrics = {
            "weighted_overall_score": 1.0,
            "_stickler_counts": {"tp": 1, "fp": 0, "tn": 0, "fn": 0},
        }
        counts = {"tp": 1, "fp": 0, "fn": 0, "tn": 0, "fp1": 0, "fp2": 0}
        return SectionEvaluationResult(sid, "Form", [], metrics), counts

    service = _service(process, workers=2)
    with _no_doc_split(), pytest.raises(TransientError) as raised:
        service.evaluate_document(
            _document("out"), _document("baseline"), store_results=False
        )
    assert raised.value.__cause__ is throttle
    assert "4" not in started and "5" not in started, started


@pytest.mark.unit
def test_a_deterministic_section_failure_is_recorded_and_the_rest_scored():
    """Only a transient failure stops the document; any other is recorded against
    its section and the remaining sections are still folded in."""

    def process(actual_section, _expected_section, _doc_ctx):
        if actual_section.section_id == "2":
            raise ValueError("bad section")
        metrics = {
            "weighted_overall_score": 1.0,
            "_stickler_counts": {"tp": 1, "fp": 0, "tn": 0, "fn": 0},
        }
        counts = {"tp": 1, "fp": 0, "fn": 0, "tn": 0, "fp1": 0, "fp2": 0}
        return SectionEvaluationResult(
            actual_section.section_id, "Form", [], metrics
        ), counts

    service = _service(process, workers=1)
    actual = _document("out")

    with _no_doc_split():
        result = service.evaluate_document(
            actual, _document("baseline"), store_results=False
        ).evaluation_result
    assert result is not None
    assert [s.section_id for s in result.section_results] == ["1", "3", "4", "5"]
    assert any("section 2" in e for e in actual.errors)
