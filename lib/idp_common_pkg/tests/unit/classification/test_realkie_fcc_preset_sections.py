# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The RealKIE-FCC-Verified configurations classify a file as one Invoice section.

The test set's ground truth is one ``Invoice`` section per file spanning every
page. Under the default ``sectionSplitting: llm_determined``, page-level
classification asks the model about every page of a multi-page file, and a page
it calls the start of a document splits the invoice. Each configuration shipped
for the test set sets ``sectionSplitting: disabled``; with its single class that
needs no model call at all.

The configurations are merged with the system defaults, as the stack merges them
before storing them, and discovered from the preset's directories rather than
listed.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from idp_common.classification.models import (
    DocumentClassification,
    PageClassification,
)
from idp_common.classification.service import ClassificationService
from idp_common.config.merge_utils import merge_config_with_defaults
from idp_common.config.models import IDPConfig
from idp_common.models import Document, Page, Status

CONFIG_LIBRARY = Path(__file__).resolve().parents[5] / "config_library"
PRESET_DIRS = (
    "unified/realkie-fcc-verified",
    "managed_config/realkie-fcc-verified",
)


def _preset_files():
    return [
        pytest.param(path, id=str(path.relative_to(CONFIG_LIBRARY)))
        for directory in PRESET_DIRS
        for path in sorted((CONFIG_LIBRARY / directory).glob("*.yaml"))
    ]


def _stored_config(raw: dict) -> IDPConfig:
    raw.pop("description", None)
    return IDPConfig(
        **merge_config_with_defaults(raw, pattern="pattern-2", validate=False)
    )


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _service(config: IDPConfig) -> ClassificationService:
    with patch("boto3.Session"):
        return ClassificationService(
            region="us-east-1", config=config, backend="bedrock"
        )


def _document(page_count: int) -> Document:
    doc = Document(
        id="fcc-invoice", input_key="fcc-invoice.pdf", status=Status.CLASSIFYING
    )
    for number in range(1, page_count + 1):
        page_id = str(number)
        doc.pages[page_id] = Page(
            page_id=page_id,
            image_uri=f"s3://bucket/{page_id}.jpg",
            parsed_text_uri=f"s3://bucket/{page_id}.md",
            raw_text_uri=f"s3://bucket/{page_id}.json",
        )
    return doc


@pytest.mark.unit
def test_the_preset_directories_hold_configurations():
    found = {param.id for param in _preset_files()}
    assert {
        "unified/realkie-fcc-verified/config.yaml",
        "managed_config/realkie-fcc-verified/config.yaml",
    } <= found, f"preset configurations not discovered; found {sorted(found)}"


@pytest.mark.unit
@pytest.mark.parametrize("path", _preset_files())
def test_every_page_lands_in_one_invoice_section_without_a_model_call(path: Path):
    """Eleven pages, so ids 10 and 11 would expose a string sort of the page ids."""
    config = _stored_config(_load(path))
    assert config.classification.sectionSplitting == "disabled"

    service = _service(config)
    document = _document(11)
    with (
        patch.object(service, "classify_page") as classify_page,
        patch.object(service, "classify_page_bedrock") as classify_page_bedrock,
    ):
        result = service.classify_document(document)

    classify_page.assert_not_called()
    classify_page_bedrock.assert_not_called()
    assert [
        (section.classification, section.page_ids) for section in result.sections
    ] == [("Invoice", [str(n) for n in range(1, 12)])]
    assert {
        (page.classification, page.confidence) for page in result.pages.values()
    } == {("Invoice", 1.0)}


@pytest.mark.unit
@pytest.mark.parametrize("path", _preset_files())
def test_without_the_setting_a_page_called_a_start_splits_the_invoice(path: Path):
    """The default strategy reproduces the split: pages [1, 2], [3, 4] and [5]."""
    raw = _load(path)
    raw.setdefault("classification", {}).pop("sectionSplitting", None)
    config = _stored_config(raw)
    assert config.classification.sectionSplitting == "llm_determined"

    starts = {"1", "3", "5"}

    def classify_page(page_id, *args, **kwargs):
        return PageClassification(
            page_id=page_id,
            classification=DocumentClassification(
                doc_type="Invoice",
                confidence=0.9,
                metadata={
                    "document_boundary": "start" if page_id in starts else "continue"
                },
            ),
        )

    service = _service(config)
    with patch.object(
        service, "classify_page", side_effect=classify_page
    ) as classify_page_mock:
        result = service.classify_document(_document(5))

    assert classify_page_mock.call_count == 5
    assert [section.page_ids for section in result.sections] == [
        ["1", "2"],
        ["3", "4"],
        ["5"],
    ]
