# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Run the document assembly handlers the way the state machine calls them.

Three task states call the two handlers: the Pipeline assembly
(``processresults_function``), the BDA assembly and the BDA assembly of a
document that already has its data (``bda_processresults_function``). Each
runner sends exactly the Parameters its state sends; ``routing_step`` adds the
literal the definition with a routing step sends.
"""

from __future__ import annotations

import io
import json
import logging
from unittest.mock import MagicMock

from idp_common.models import Document, Section, Status
from tests.unit.lambdas._goldens import load_handler
from tests.unit.lambdas._review_env import (
    BUCKET,
    EXEC,
    KEY,
    result_uri,
    swapped,
)

processresults_index = load_handler(
    "assembly_processresults_index", "processresults_function/index.py"
)
bda_index = load_handler(
    "assembly_bda_processresults_index",
    "bda_processresults_function/index.py",
    extra_modules={"pypdfium2": MagicMock()},
)

logger = logging.getLogger(__name__)

#: The task shapes that assemble a document, by the name the tests use.
TASKS = ("pipeline", "bda", "bda_skip")


def assembled(response: dict) -> dict:
    """The document a task returned, in full.

    A task returns the document inline, or a reference to a stored copy whose
    key carries the time it was written; reading it gives the same form for both.
    """
    return Document.load_document(response["document"], BUCKET, logger).to_dict()


def document_service(existing=None) -> MagicMock:
    """A tracking-row service whose row is ``existing`` (``None``: no row)."""
    service = MagicMock()
    service.get_document.return_value = existing
    return service


def section_result(section: Section, *, name_section: bool = True) -> dict:
    """One Map result: the document of one section, as the section steps return it."""
    doc = Document(
        id=KEY,
        input_bucket="in",
        input_key=KEY,
        output_bucket=BUCKET,
        status=Status.ASSESSING,
        sections=[section],
    )
    result = {"document": doc.to_dict()}
    if name_section:
        result["section_id"] = section.section_id
    return result


def run_pipeline(
    store,
    base: Document,
    results: list,
    *,
    cfg,
    routing_step: bool = False,
    service=None,
) -> dict:
    """The Pipeline assembly task."""
    event = {
        "execution_arn": EXEC,
        "ClassificationResult": {"document": base.to_dict()},
        "ExtractionResults": results,
    }
    if routing_step:
        event["routing_step"] = True
    with swapped(
        processresults_index,
        get_config=lambda **kw: cfg,
        create_document_service=lambda *a, **kw: service or document_service(),
        create_metadata_file=lambda *a, **kw: None,
        _REVIEW_STORE=store,
    ):
        return processresults_index.handler(event, None)


def run_bda_skip(
    store, doc: Document, *, cfg, routing_step: bool = False, service=None
) -> dict:
    """The BDA assembly task of a document that already has its data."""
    event = {
        "execution_arn": EXEC,
        "output_bucket": BUCKET,
        "skip_bda": True,
        "document": doc.to_dict(),
    }
    if routing_step:
        event["routing_step"] = True
    with swapped(
        bda_index,
        get_config=lambda **kw: cfg,
        create_document_service=lambda *a, **kw: service or document_service(),
        _REVIEW_STORE=store,
    ):
        return bda_index.handler(event, None)


def run_bda(
    store,
    *,
    cfg,
    routing_step: bool = False,
    field_confidence: float = 0.4,
    service=None,
) -> dict:
    """The BDA assembly task, over one custom-output segment of one page.

    ``field_confidence`` is the confidence BDA gave the one field: below the
    configured threshold it is a confidence alert on the section.
    """
    event = {
        "execution_arn": EXEC,
        "output_bucket": BUCKET,
        "BDAResponse": {
            "job_detail": {
                "input_s3_object": {"name": KEY, "s3_bucket": "in"},
                "output_s3_location": {"s3_bucket": "bda", "name": "jobs/j1/0"},
            }
        },
    }
    if routing_step:
        event["routing_step"] = True
    custom_output = {
        "matched_blueprint": {"name": "invoice-bp", "confidence": 0.95},
        "split_document": {"page_indices": [0]},
        "document_class": {"type": "Invoice"},
        "explainability_info": [
            {
                "Total": {
                    "value": "100",
                    "confidence": field_confidence,
                    "geometry": [{"page": 1}],
                }
            }
        ],
        "inference_result": {"Total": "100"},
    }
    job_metadata = {
        "output_metadata": [
            {
                "segment_metadata": [
                    {
                        "custom_output_status": "MATCH",
                        "custom_output_path": "s3://bda/jobs/j1/0/custom_output/0/result.json",
                        "standard_output_path": "s3://bda/jobs/j1/0/standard_output/0/result.json",
                    }
                ]
            }
        ]
    }
    s3_client = MagicMock()
    s3_client.get_object.side_effect = lambda **kw: {
        "Body": io.BytesIO(json.dumps(job_metadata).encode())
    }

    def sections(bucket, prefix, output_bucket, object_key, document, threshold, c):
        document.sections = [
            Section(
                section_id="1",
                classification="Invoice",
                page_ids=["1"],
                extraction_result_uri=result_uri("1"),
            )
        ]
        document.num_pages = 1
        return document

    existing = Document(id=KEY, input_key=KEY, config_version="default")
    with swapped(
        bda_index,
        get_config=lambda **kw: cfg,
        create_document_service=lambda *a, **kw: service or document_service(existing),
        create_pdf_page_images=lambda *a, **kw: 1,
        process_bda_sections=sections,
        process_bda_pages=lambda *a, **kw: a[4],
        s3_client=s3_client,
        download_json=lambda bucket, key: custom_output,
        download_decimal=lambda bucket, key: custom_output,
        _REVIEW_STORE=store,
    ):
        with swapped(bda_index.metrics, put_metric=lambda *a, **kw: None):
            return bda_index.handler(event, None)
