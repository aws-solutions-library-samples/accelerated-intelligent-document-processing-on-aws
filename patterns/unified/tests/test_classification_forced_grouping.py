# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The classification step skips the model whenever something was forced (#1699).

A forced section grouping — one section of a test-set packet re-extracted under a
corrected class — builds every section the ground truth records. Its pages need not
cover the document: a page the ground truth never placed in a section keeps no class.
The step used to decide "skip the model" only from "every page has a class", so such
a document would have been classified from scratch, replacing the forced sections and
regrouping every section the reviewer meant to keep.

Driven through the real handler, with the document service, configuration, metering
and serialization stubbed; ``ClassificationService`` is a mock so a call to it is the
observable failure.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from idp_common.forced_sections import encode_forced_sections
from idp_common.models import Document, Page, Status

_HANDLER = Path(__file__).resolve().parents[1] / "src/classification_function/index.py"
# Named as one literal so test_handler_imports_are_region_free.py, which derives the
# handlers it checks from literal paths in test files, covers this handler as well.
_LITERAL_PATH_FOR_DISCOVERY = "patterns/unified/src/classification_function/index.py"


def _load():
    """Import the handler with X-Ray disabled and its global patching suppressed.

    ``patch_all()`` at module scope would patch botocore for every later test in the
    session, so it is replaced while the module loads.
    """
    with (
        patch.dict(os.environ, {"AWS_XRAY_SDK_ENABLED": "false"}),
        patch("aws_xray_sdk.core.patch_all"),
    ):
        if str(_HANDLER.parent) not in sys.path:
            sys.path.insert(0, str(_HANDLER.parent))
        spec = importlib.util.spec_from_file_location(
            "classification_function_forced_grouping", _HANDLER
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


@pytest.fixture(scope="module")
def handler_module():
    return _load()


def _document(pages, forced_sections=None, forced_class=None):
    doc = Document(
        id="run1/packet.pdf",
        input_key="run1/packet.pdf",
        output_bucket="out",
        status=Status.OCR,
        forced_sections=forced_sections,
        forced_document_class=forced_class,
    )
    doc.pages = {str(n): Page(page_id=str(n)) for n in range(1, pages + 1)}
    return doc


def _invoke(module, doc):
    """Run the handler on ``doc``; return (the service mock, the document)."""
    service = MagicMock()
    service.return_value.classify_document.side_effect = lambda d: d
    with (
        patch.dict(os.environ, {"AWS_XRAY_SDK_ENABLED": "false"}),
        patch.object(module.Document, "load_document", return_value=doc),
        patch.object(module, "get_config", return_value=MagicMock(model_dump=dict)),
        patch.object(module, "create_document_service"),
        patch.object(module, "calculate_lambda_metering", return_value={}),
        patch.object(module, "merge_metering_data", return_value={}),
        patch.object(module, "metrics"),
        patch.object(module.classification, "ClassificationService", service),
        patch.object(Document, "serialize_document", return_value={"serialized": 1}),
    ):
        module.handler({"OCRResult": {"document": {}}, "execution_arn": "arn"}, None)
    return service, doc


@pytest.mark.unit
class TestTheModelIsSkippedWhenAGroupingIsForced:
    GROUPING = [
        {"section_id": "1", "classification": "Payslip", "page_indices": [0, 1]},
        {"section_id": "2", "classification": "W2", "page_indices": [3]},
    ]

    def test_a_grouping_that_leaves_a_page_unplaced_still_skips_the_model(
        self, handler_module
    ):
        # Page 3 (index 2) is in no section.
        doc = _document(4, forced_sections=encode_forced_sections(self.GROUPING))

        service, doc = _invoke(handler_module, doc)

        service.assert_not_called()
        assert [(s.section_id, s.classification, s.page_ids) for s in doc.sections] == [
            ("1", "Payslip", ["1", "2"]),
            ("2", "W2", ["4"]),
        ]
        assert doc.pages["3"].classification is None

    def test_a_whole_document_class_still_skips_the_model(self, handler_module):
        doc = _document(2, forced_class="W2")

        service, doc = _invoke(handler_module, doc)

        service.assert_not_called()
        assert [s.page_ids for s in doc.sections] == [["1", "2"]]

    def test_an_ordinary_document_is_still_classified(self, handler_module):
        """The control: with nothing forced the model runs, so the skip above is the
        forced path's doing and not the stubs'."""
        doc = _document(2)

        service, _doc = _invoke(handler_module, doc)

        service.return_value.classify_document.assert_called_once()
