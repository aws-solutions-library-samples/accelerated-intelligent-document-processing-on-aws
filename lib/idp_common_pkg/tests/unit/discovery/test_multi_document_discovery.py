# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for multi-document discovery config saving."""

from unittest.mock import MagicMock, patch

import pytest

from idp_common.discovery.discovery_agent import DiscoveredClass
from idp_common.discovery.multi_document_discovery import MultiDocumentDiscovery


@pytest.mark.unit
class TestSaveToConfigReportedNames:
    """The names returned by ``save_to_config`` must exist in the config.

    ``_merge_and_save_class`` normalizes the class id in place, so the raw LLM
    label in ``DiscoveredClass.classification`` no longer names what was saved.
    Reporting it would tell the user about a class the config does not contain.
    """

    @pytest.fixture
    def discovery(self):
        with patch(
            "idp_common.discovery.multi_document_discovery.BedrockClient"
        ) as mock_client:
            mock_client.return_value = MagicMock()
            return MultiDocumentDiscovery(region="us-west-2")

    def _save(self, discovery, discovered_class):
        """Run save_to_config with the real _merge_and_save_class mutation."""
        with patch(
            "idp_common.discovery.classes_discovery.ClassesDiscovery"
        ) as mock_cd:
            instance = mock_cd.return_value
            # Mirror the in-place normalization the real merge performs.
            instance._merge_and_save_class.side_effect = lambda schema: schema.update(
                {"$id": "Bank-Statement", "x-aws-idp-document-type": "Bank-Statement"}
            )
            return discovery.save_to_config(
                discovered_classes=[discovered_class],
                config_version="v1",
                input_bucket="b",
                input_prefix="p/",
            )

    def test_reported_name_is_the_saved_id_not_the_raw_label(self, discovery):
        saved = self._save(
            discovery,
            DiscoveredClass(
                cluster_id=0,
                classification="Bank Statement",
                json_schema={
                    "$id": "Bank Statement",
                    "x-aws-idp-document-type": "Bank Statement",
                    "type": "object",
                    "properties": {},
                },
                document_count=3,
                sample_doc_ids=[0, 1, 2],
            ),
        )

        assert saved == ["Bank-Statement"]

    def test_classification_is_sanitized_when_the_schema_carries_no_id(self, discovery):
        """Fallback path: whatever is reported must still be a valid class id."""
        with patch(
            "idp_common.discovery.classes_discovery.ClassesDiscovery"
        ) as mock_cd:
            mock_cd.return_value._merge_and_save_class.return_value = None
            saved = discovery.save_to_config(
                discovered_classes=[
                    DiscoveredClass(
                        cluster_id=7,
                        classification="Bank Statement",
                        json_schema={"type": "object", "properties": {}},
                        document_count=1,
                        sample_doc_ids=[0],
                    )
                ],
                config_version="v1",
                input_bucket="b",
                input_prefix="p/",
            )

        assert saved == ["Bank-Statement"]
