# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``document_shape.maxPagesForClassification`` applies only to a one-document file.

A page limit gives the whole file one class from its first N pages. With boundary
detection on, it used to collapse only files longer than N while sectioning
shorter files normally, so one setting gave a different answer per file in the
same run. In config format 0.9 the limit applies when ``sectionSplitting`` is
``disabled`` and is ignored otherwise; the v0.8 -> v0.9 migration converts a
legacy combination of the two to ``disabled``, which keeps the old behaviour of
every migrated profile.
"""

import logging

import pytest

from idp_common.classification.service import ClassificationService


def _service(document_shape):
    return ClassificationService(
        region="us-west-2",
        config={
            "classes": [{"$id": "invoice", "description": "an invoice"}],
            "classification": {
                "one_stage": {
                    "page": {
                        "system_prompt": "s",
                        "task_prompt": "{DOCUMENT_TEXT}",
                    }
                },
                "document_shape": document_shape,
            },
        },
    )


@pytest.mark.unit
class TestThePageLimitNeedsOneDocumentPerFile:
    def test_a_limit_with_disabled_splitting_is_applied(self):
        service = _service(
            {"sectionSplitting": "disabled", "maxPagesForClassification": "3"}
        )
        assert service.max_pages_for_classification == "3"

    @pytest.mark.parametrize("splitting", ["llm_determined", "page"])
    def test_a_limit_with_boundary_detection_is_ignored_and_says_so(
        self, splitting, caplog
    ):
        with caplog.at_level(logging.WARNING):
            service = _service(
                {"sectionSplitting": splitting, "maxPagesForClassification": "3"}
            )
        assert service.max_pages_for_classification == "ALL"
        assert "maxPagesForClassification" in caplog.text
        assert "ignored" in caplog.text

    def test_all_is_never_reported(self, caplog):
        with caplog.at_level(logging.WARNING):
            service = _service({"sectionSplitting": "llm_determined"})
        assert service.max_pages_for_classification == "ALL"
        assert "maxPagesForClassification" not in caplog.text

    def test_a_legacy_flat_combination_keeps_its_limit(self):
        """The migration turns `limit + llm_determined` into `disabled`, so a
        0.6.12 profile keeps classifying from its first N pages."""
        service = ClassificationService(
            region="us-west-2",
            config={
                "classes": [{"$id": "invoice", "description": "an invoice"}],
                "classification": {
                    "system_prompt": "s",
                    "task_prompt": "{DOCUMENT_TEXT}",
                    "sectionSplitting": "llm_determined",
                    "maxPagesForClassification": "2",
                },
            },
        )
        assert service.max_pages_for_classification == "2"
        assert (
            service.config.classification.document_shape.sectionSplitting == "disabled"
        )
