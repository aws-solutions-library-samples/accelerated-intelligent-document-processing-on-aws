# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A reprocess must keep every section, and must not re-extract one (#1686).

Two halves of one reprocess shape: a finished Pipeline-mode document is
resubmitted, so every section already has an ``extraction_result_uri``.

**Sections.** ProcessResults rebuilds ``document.sections`` from the per-section
results of the ``ProcessSections`` map. It took ``sections[0]`` from each result,
which assumes every result document holds exactly the one section it is about.
The extraction branch that runs and the assessment skip branch both narrow the
document to that section; the extraction skip branch does not. So when extraction
was skipped and assessment then ran (no explainability in the stored result, for
instance with confidence scoring off), every result still held all N sections and
the rebuilt document was N copies of section 1. ProcessResults now picks the
section the result names (``section_id``, which every map iteration returns).

**Sharding.** With the Step Functions extraction runtime, the map's first state is
the shard planner, and when it answers ``shard_mode: true`` the section goes to
the shard map and is extracted again from scratch: the extraction skip branch is
only on the single-pass route. The planner never looked at
``extraction_result_uri``, so a reprocess of a section large enough to shard paid
for its extraction a second time. It now plans single-pass for a section that is
already extracted, which routes it to the extraction step and its skip branch.

Both are driven through the real handlers and the real service. The stubs are the
outbound edges only: configuration, the document service, the S3 read of the
stored extraction result, and (for the planner) the page-text and page-image
loaders.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from idp_common.config.models import IDPConfig
from idp_common.extraction.service import ExtractionService
from idp_common.models import Document, Page, Section, Status
from tests.unit.lambdas._goldens import load_handler

extraction_index = load_handler(
    "reprocess_sections_extraction_index", "extraction_function/index.py"
)
assessment_index = load_handler(
    "reprocess_sections_assessment_index", "assessment_function/index.py"
)
processresults_index = load_handler(
    "reprocess_sections_processresults_index", "processresults_function/index.py"
)

OBJECT_KEY = "folder/packet.pdf"
EXEC = "arn:aws:states:us-east-1:111122223333:execution:sm:exec-2"
CLASSES = ["w2", "bank-statement", "payslip"]


class _Context:
    function_name = "fn"
    memory_limit_in_mb = 1024
    invoked_function_arn = "arn:aws:lambda:us-east-1:111122223333:function:fn"
    aws_request_id = "req-1"

    def get_remaining_time_in_millis(self):
        return 300_000


def _stored_document() -> Document:
    """A finished three-section document, as the reprocess message delivers it."""
    return Document(
        id=OBJECT_KEY,
        input_bucket="in",
        input_key=OBJECT_KEY,
        output_bucket="out",
        status=Status.CLASSIFYING,
        num_pages=len(CLASSES),
        config_version="default",
        pages={str(i): Page(page_id=str(i)) for i in range(1, len(CLASSES) + 1)},
        sections=[
            Section(
                section_id=str(i),
                classification=cls,
                page_ids=[str(i)],
                extraction_result_uri=f"s3://out/{OBJECT_KEY}/sections/{i}/result.json",
            )
            for i, cls in enumerate(CLASSES, start=1)
        ],
    )


@pytest.fixture
def wired(monkeypatch):
    # Empty working bucket: serialize_document returns the document inline.
    monkeypatch.setenv("WORKING_BUCKET", "")
    # Confidence scoring off: the assessment step runs (nothing to skip on) and
    # the real AssessmentService returns the section without calling a model.
    config = IDPConfig()
    config.extraction.confidence.enabled = False
    for module in (extraction_index, assessment_index, processresults_index):
        monkeypatch.setattr(module, "get_config", lambda **kw: config)
        doc_service = MagicMock()
        doc_service.get_document.return_value = None
        monkeypatch.setattr(
            module, "create_document_service", lambda *a, _s=doc_service, **kw: _s
        )
    monkeypatch.setattr(
        processresults_index, "create_metadata_file", lambda *a, **kw: None
    )
    monkeypatch.setattr(processresults_index, "_REVIEW_STORE", None)


def _run_reprocess(document: Document) -> Document:
    """ProcessSections (extraction then assessment per section), then ProcessResults."""
    classification_result = {"document": document.to_dict()}
    results = []
    for section in document.sections:
        extracted = extraction_index.handler(
            {
                "execution_arn": EXEC,
                "document": classification_result["document"],
                "section_id": section.section_id,
            },
            _Context(),
        )
        results.append(
            assessment_index.handler(
                {
                    "execution_arn": EXEC,
                    "document": extracted["document"],
                    "section_id": section.section_id,
                },
                _Context(),
            )
        )
    result = processresults_index.handler(
        {
            "ClassificationResult": classification_result,
            "ExtractionResults": results,
            "execution_arn": EXEC,
        },
        _Context(),
    )
    return Document.load_document(result["document"], "", None)


@pytest.mark.unit
def test_reprocess_with_assessment_running_keeps_every_section(wired, monkeypatch):
    # The stored result has no explainability, so assessment is not skipped.
    reads = []

    def _stored_result(uri):
        reads.append(uri)
        return {"inference_result": {"Total": "1.00"}}

    monkeypatch.setattr(assessment_index.s3, "get_json_content", _stored_result)

    final = _run_reprocess(_stored_document())

    # The assessment skip check really ran, and found nothing to skip on.
    assert len(reads) == len(CLASSES)
    assert [s.section_id for s in final.sections] == ["1", "2", "3"]
    assert [s.classification for s in final.sections] == CLASSES


@pytest.mark.unit
def test_reprocess_with_assessment_skipped_keeps_every_section(wired, monkeypatch):
    """The shape that already worked, kept so the fix cannot trade one for the other."""
    monkeypatch.setattr(
        assessment_index.s3,
        "get_json_content",
        lambda uri: {
            "inference_result": {"Total": "1.00"},
            "explainability_info": [{"Total": {"confidence": 0.99}}],
        },
    )

    final = _run_reprocess(_stored_document())

    assert [s.section_id for s in final.sections] == ["1", "2", "3"]
    assert [s.classification for s in final.sections] == CLASSES


# ---------------------------------------------------------------------------
# The shard planner
# ---------------------------------------------------------------------------


def _sharding_service(monkeypatch) -> ExtractionService:
    """A service configured so a dense multi-page section really shards."""
    cfg = {
        "extraction": {
            "task_prompt": "Extract from:\n{DOCUMENT_TEXT}\nEnd.",
            "agentic": {
                "enabled": True,
                "runtime": "step_functions",
                "max_concurrent_batches": 4,
                "shard_token_budget": 5000,
            },
        },
        "classes": [
            {
                "$id": "bank-statement",
                "type": "object",
                "properties": {"Total": {"type": "string"}},
            }
        ],
    }
    service = ExtractionService(region="us-east-1", config=cfg)
    # The I/O edges: page text and page images would be read from S3.
    monkeypatch.setattr(
        service,
        "_load_page_texts",
        lambda document, page_ids: {
            pid: f"PAGE{pid} " + "w" * 12000 for pid in page_ids
        },
    )
    monkeypatch.setattr(service, "_load_document_images", lambda *a, **kw: [])
    return service


def _large_section_document(extraction_result_uri: str | None) -> Document:
    page_ids = [str(i) for i in range(1, 7)]
    return Document(
        id=OBJECT_KEY,
        input_bucket="in",
        input_key=OBJECT_KEY,
        output_bucket="out",
        num_pages=len(page_ids),
        pages={pid: Page(page_id=pid) for pid in page_ids},
        sections=[
            Section(
                section_id="1",
                classification="bank-statement",
                page_ids=page_ids,
                extraction_result_uri=extraction_result_uri,
            )
        ],
    )


@pytest.mark.unit
@pytest.mark.parametrize("uri", [None, "", "   "])
def test_planner_shards_a_large_section_that_has_not_been_extracted(monkeypatch, uri):
    """Non-vacuity: the fixture is large enough to shard at all.

    A blank URI is "not extracted" to the extraction handler's skip check, so it
    must be to the planner as well.
    """
    plan = _sharding_service(monkeypatch).plan_section_shards(
        document=_large_section_document(uri), section_id="1"
    )
    assert plan["shard_mode"] is True
    assert plan["num_shards"] > 1


@pytest.mark.unit
def test_planner_does_not_shard_a_section_that_is_already_extracted(monkeypatch):
    service = _sharding_service(monkeypatch)
    plan = service.plan_section_shards(
        document=_large_section_document(
            f"s3://out/{OBJECT_KEY}/sections/1/result.json"
        ),
        section_id="1",
    )
    assert plan["shard_mode"] is False
    assert plan["shards"] == []
