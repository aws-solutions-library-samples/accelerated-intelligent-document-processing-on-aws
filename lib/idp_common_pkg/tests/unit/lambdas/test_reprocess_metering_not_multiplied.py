# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A reprocess must not multiply the document's stored metering (#1665).

Finishing a legacy HITL review (Process Changes, Skip All Reviews, completing the
last section) resubmits the stored document, ``Metering`` included, and the
Pipeline-mode workflow then skips every step whose output already exists. The
document's metering reaches ``Pipeline_ProcessResultsStep`` by two routes:

* ``ClassificationResult.document.metering`` — the stored metering plus whatever
  the OCR and classification skip branches added; and
* one ``ExtractionResults`` entry per section, each meant to carry only what that
  section's Map iteration spent, which ProcessResults merges on top.

The extraction skip branch returned the whole incoming document with its metering
untouched, while the extraction branch that actually runs starts the section
document from ``{}``. The assessment skip branch then copies the incoming metering
(deliberately: an agentic section reaches it with extraction's Bedrock cost on it),
so every skipped section carried a full copy of the stored metering into
ProcessResults, and an N-section document came out at (N+1) times its real cost
with no model call repeated.

This drives the three REAL handlers in the order the state machine runs them —
extraction (skip) then assessment (skip) per section, then ProcessResults over the
collected results — and asserts the exact merged metering. Only the outbound edges
are stubbed: configuration, the document service, the S3 read the assessment skip
check makes, and ``calculate_lambda_metering`` (replaced by a fixed value per step
so the expected total is exact rather than time-dependent).
"""

from __future__ import annotations

import copy
from unittest.mock import MagicMock

import pytest

from idp_common.config.models import IDPConfig
from idp_common.models import Document, Section, Status
from tests.unit.lambdas._goldens import load_handler

extraction_index = load_handler(
    "reprocess_metering_extraction_index", "extraction_function/index.py"
)
assessment_index = load_handler(
    "reprocess_metering_assessment_index", "assessment_function/index.py"
)
processresults_index = load_handler(
    "reprocess_metering_processresults_index", "processresults_function/index.py"
)

OBJECT_KEY = "folder/statement.pdf"
EXEC = "arn:aws:states:us-east-1:111122223333:execution:sm:exec-2"

#: What the first run stored. The Bedrock and Textract entries are the ones a
#: reprocess must leave exactly as they were: no model or OCR call is repeated.
STORED_METERING = {
    "OCR/textract/analyze_document-Layout": {"pages": 3},
    "Classification/bedrock/us.amazon.nova-pro-v1:0": {
        "inputTokens": 4000,
        "outputTokens": 120,
        "totalTokens": 4120,
    },
    "Extraction/bedrock/us.anthropic.claude-sonnet-4-6": {
        "inputTokens": 9000,
        "outputTokens": 1500,
        "totalTokens": 10500,
    },
    "Assessment/bedrock/us.anthropic.claude-sonnet-4-6": {
        "inputTokens": 7000,
        "outputTokens": 900,
        "totalTokens": 7900,
    },
    "Extraction/lambda/requests": {"requests": 3},
    "Extraction/lambda/duration": {"gb_seconds": 30.0},
}


class _Context:
    function_name = "fn"
    memory_limit_in_mb = 1024
    invoked_function_arn = "arn:aws:lambda:us-east-1:111122223333:function:fn"
    aws_request_id = "req-1"

    def get_remaining_time_in_millis(self):
        return 300_000


def _lambda_metering(component, context, start_time):
    """A fixed per-invocation Lambda charge, so the expected total is exact."""
    return {
        f"{component}/lambda/requests": {"requests": 1},
        f"{component}/lambda/duration": {"gb_seconds": 0.5},
    }


def _stored_document(num_sections: int) -> Document:
    """The document as the reprocess message delivers it: every step done."""
    return Document(
        id=OBJECT_KEY,
        input_bucket="in",
        input_key=OBJECT_KEY,
        output_bucket="out",
        status=Status.CLASSIFYING,
        num_pages=num_sections,
        config_version="default",
        sections=[
            Section(
                section_id=str(i),
                classification="bank-statement",
                page_ids=[str(i)],
                extraction_result_uri=f"s3://out/{OBJECT_KEY}/sections/{i}/result.json",
            )
            for i in range(1, num_sections + 1)
        ],
        metering=copy.deepcopy(STORED_METERING),
    )


@pytest.fixture
def wired(monkeypatch):
    # Empty working bucket: serialize_document returns the document inline.
    monkeypatch.setenv("WORKING_BUCKET", "")
    config = IDPConfig()
    for module in (extraction_index, assessment_index, processresults_index):
        monkeypatch.setattr(module, "get_config", lambda **kw: config)
        doc_service = MagicMock()
        doc_service.get_document.return_value = None
        monkeypatch.setattr(
            module, "create_document_service", lambda *a, _s=doc_service, **kw: _s
        )
    for module in (extraction_index, assessment_index):
        monkeypatch.setattr(module, "calculate_lambda_metering", _lambda_metering)
    # The assessment skip check: extraction results already carry explainability.
    monkeypatch.setattr(
        assessment_index.s3,
        "get_json_content",
        lambda uri: {
            "inference_result": {"Balance": "1.00"},
            "explainability_info": [{"Balance": {"confidence": 0.99}}],
        },
    )
    monkeypatch.setattr(
        processresults_index, "create_metadata_file", lambda *a, **kw: None
    )
    monkeypatch.setattr(processresults_index, "_REVIEW_STORE", None)


def _run_reprocess(document: Document) -> Document:
    classification_result = {"document": document.to_dict()}
    extraction_results = []
    for section in document.sections:
        extracted = extraction_index.handler(
            {
                "execution_arn": EXEC,
                "document": classification_result["document"],
                "section_id": section.section_id,
            },
            _Context(),
        )
        assessed = assessment_index.handler(
            {
                "execution_arn": EXEC,
                "document": extracted["document"],
                "section_id": section.section_id,
            },
            _Context(),
        )
        extraction_results.append(assessed)
    result = processresults_index.handler(
        {
            "ClassificationResult": classification_result,
            "ExtractionResults": extraction_results,
            "execution_arn": EXEC,
        },
        _Context(),
    )
    return Document.load_document(result["document"], "", None)


def _expected(num_sections: int) -> dict:
    """The stored metering plus one Lambda charge per skipped step, nothing else.

    Written out arithmetically rather than built with ``merge_metering_data``, the
    function the handlers merge with, so a merge that overwrote instead of adding
    could not produce the expected value as well as the actual one.
    """
    expected = copy.deepcopy(STORED_METERING)
    expected["Extraction/lambda/requests"] = {"requests": 3 + num_sections}
    expected["Extraction/lambda/duration"] = {"gb_seconds": 30.0 + 0.5 * num_sections}
    expected["Assessment/lambda/requests"] = {"requests": num_sections}
    expected["Assessment/lambda/duration"] = {"gb_seconds": 0.5 * num_sections}
    return expected


@pytest.mark.unit
@pytest.mark.parametrize("num_sections", [1, 3])
def test_reprocess_with_every_step_skipped_keeps_stored_metering(wired, num_sections):
    final = _run_reprocess(_stored_document(num_sections))

    # Every Bedrock and Textract figure is exactly what the first run stored.
    for key, value in STORED_METERING.items():
        if "/lambda/" in key:
            continue
        assert final.metering[key] == value, (
            f"{key} is {final.metering[key]} after a reprocess that made no "
            f"model or OCR call; stored {value}"
        )
    # And the whole map is the stored metering plus this run's Lambda charges.
    assert final.metering == _expected(num_sections)


@pytest.mark.unit
def test_extraction_skip_returns_only_its_own_metering(wired):
    """The per-section result carries this invocation's charge, not the document's.

    ProcessResults merges every section result onto the document metering it
    already holds, so anything the document carried in is counted again.
    """
    document = _stored_document(2)
    extracted = extraction_index.handler(
        {"execution_arn": EXEC, "document": document.to_dict(), "section_id": "2"},
        _Context(),
    )
    section_document = Document.load_document(extracted["document"], "", None)

    assert section_document.metering == _lambda_metering("Extraction", None, None)
    # The skip branch still hands assessment the section it was asked about.
    assert "2" in [s.section_id for s in section_document.sections]
