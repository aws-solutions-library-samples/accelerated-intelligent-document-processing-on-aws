# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""How the RealKIE-FCC-Verified configurations divide a file into sections.

The test set's ground truth is one ``Invoice`` section per file spanning every
page. Under the default ``sectionSplitting: llm_determined``, page-level
classification asks the model about every page of a multi-page file, and a page
it calls the start of a document splits the invoice. The stack-managed
``realkie-fcc-verified`` profile and the 1S-TopK reference configuration set
``sectionSplitting: disabled``; with their single class that needs no model call
at all.

The deploy-time preset, ``unified/realkie-fcc-verified/config.yaml``, leaves the
key unset. A stack deployed with ``ConfigurationPreset=realkie-fcc-verified``
rebuilds its ``default`` profile from that file on every update, and ``default``
is usually the active profile and the base an imported or newly uploaded profile
is built on. Leaving the key unset keeps the default strategy there, so an
upgrade never changes such a stack's ``default``.

The configurations are merged with the system defaults, as the stack merges them
before storing them.
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
DEPLOY_TIME_PRESET = "unified/realkie-fcc-verified/config.yaml"
WHOLE_DOCUMENT_CONFIGS = (
    "managed_config/realkie-fcc-verified/config.yaml",
    "unified/realkie-fcc-verified/config-1s-topk-with-ocr-image.yaml",
)


def _stored_config(raw: dict) -> IDPConfig:
    raw.pop("description", None)
    return IDPConfig(
        **merge_config_with_defaults(raw, pattern="pattern-2", validate=False)
    )


def _load(relative_path: str) -> dict:
    return yaml.safe_load((CONFIG_LIBRARY / relative_path).read_text(encoding="utf-8"))


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


def _invoice_pages_starting_at(starts: set[str]):
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

    return classify_page


def _sections_under_page_level_boundaries(config: IDPConfig):
    service = _service(config)
    with patch.object(
        service,
        "classify_page",
        side_effect=_invoice_pages_starting_at({"1", "3", "5"}),
    ) as classify_page:
        result = service.classify_document(_document(5))
    return classify_page.call_count, [section.page_ids for section in result.sections]


@pytest.mark.unit
@pytest.mark.parametrize("relative_path", WHOLE_DOCUMENT_CONFIGS)
def test_every_page_lands_in_one_invoice_section_without_a_model_call(
    relative_path: str,
):
    """Eleven pages, so ids 10 and 11 would expose a string sort of the page ids."""
    config = _stored_config(_load(relative_path))
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
@pytest.mark.parametrize("relative_path", WHOLE_DOCUMENT_CONFIGS)
def test_without_the_setting_a_page_called_a_start_splits_the_invoice(
    relative_path: str,
):
    """The default strategy reproduces the split: pages [1, 2], [3, 4] and [5]."""
    raw = _load(relative_path)
    classification = raw.get("classification") or {}
    classification.pop("sectionSplitting", None)
    raw["classification"] = classification
    config = _stored_config(raw)
    assert config.classification.sectionSplitting == "llm_determined"

    assert _sections_under_page_level_boundaries(config) == (
        5,
        [["1", "2"], ["3", "4"], ["5"]],
    )


@pytest.mark.unit
def test_the_deploy_time_preset_keeps_the_default_strategy():
    """A stack deployed with the preset keeps ``llm_determined`` in ``default``.

    The preset is what such a stack rebuilds ``default`` from on every update, so
    a value set here would change that profile, and the profiles later built on
    it, on upgrade. Unset, the stored profile keeps the default strategy and a
    multi-page file still goes to the model page by page for its boundaries.
    """
    raw = _load(DEPLOY_TIME_PRESET)
    assert "sectionSplitting" not in (raw.get("classification") or {})

    config = _stored_config(raw)
    assert config.classification.sectionSplitting == "llm_determined"
    assert _sections_under_page_level_boundaries(config) == (
        5,
        [["1", "2"], ["3", "4"], ["5"]],
    )
