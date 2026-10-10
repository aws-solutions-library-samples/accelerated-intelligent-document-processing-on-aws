# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Persistence of a document's configuration provenance.

A document's Configuration Profile can arrive from several places — upload
metadata, a config prefix mapping, the active-profile pointer, a reprocess
request, or a feature that pins its own — and the profile name alone does not say
which applied. `ConfigSource` / `ConfigMappingPrefix` are what make "why did this
process under `lending` r7?" answerable from the tracking row instead of from log
archaeology across two Lambdas, so they have to survive all three write paths and
the read back.

⚠️ The **create** path is the one that matters most here, and it is the one that
had nothing. It previously wrote no `ConfigVersion` at all — stamping happened on
the later `update_document` — which is fine for a document that goes on to be
processed and wrong for one refused at ingest by a `reject` prefix mapping: that
document is created and never updated, so its row carried no profile name, and
`scope_allows` denies a document with no name to match against to *every* scoped
caller. The person whose upload was refused would see no row at all rather than a
failure they could act on.
"""

from decimal import Decimal
from unittest.mock import Mock

import pytest

from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.models import Document, Status


@pytest.mark.unit
class TestTheCreatePathCarriesTheConfiguration:
    """A document refused at ingest is created and never updated."""

    def setup_method(self):
        self.service = DocumentDynamoDBService(dynamodb_client=Mock())

    def _create_item(self, **kwargs):
        doc = Document(id="test.pdf", input_key="test.pdf", **kwargs)
        return self.service._document_to_create_item(doc)

    def test_a_refused_document_is_visible_to_a_scoped_caller(self):
        """The whole point: an unnamed document is denied to every scoped user, so a
        refusal with no ConfigVersion is indistinguishable from a lost upload."""
        item = self._create_item(
            status=Status.FAILED,
            config_version="regulated",
            config_source="rejected",
            config_mapping_prefix="regulated/",
            config_assignment_error="Refused: conflicting upload metadata.",
        )
        assert item["ConfigVersion"] == "regulated"
        assert item["ConfigSource"] == "rejected"
        assert item["ConfigMappingPrefix"] == "regulated/"
        assert item["ConfigAssignmentError"].startswith("Refused:")

    def test_the_revision_is_written_as_an_int(self):
        item = self._create_item(config_version="lending", config_revision=7)
        assert item["ConfigRevision"] == 7
        assert isinstance(item["ConfigRevision"], int)

    def test_a_zero_revision_is_written_rather_than_dropped(self):
        """0 is a falsy int and a legitimate revision number, so a truthiness test
        here would silently drop it."""
        item = self._create_item(config_version="lending", config_revision=0)
        assert item["ConfigRevision"] == 0

    def test_nothing_is_written_when_nothing_is_known(self):
        """An ordinary queued document with no pin must not gain empty attributes —
        a present-but-empty ConfigVersion would read as a profile named ''."""
        item = self._create_item()
        for attribute in (
            "ConfigVersion",
            "ConfigRevision",
            "ConfigSource",
            "ConfigMappingPrefix",
            "ConfigAssignmentError",
        ):
            assert attribute not in item


@pytest.mark.unit
class TestTheUpdatePathCarriesTheProvenance:
    def setup_method(self):
        self.service = DocumentDynamoDBService(dynamodb_client=Mock())

    def _expressions(self, **kwargs):
        doc = Document(id="test.pdf", input_key="test.pdf", **kwargs)
        return self.service._document_to_update_expressions(doc)

    def test_source_and_prefix_are_persisted(self):
        _expr, names, values = self._expressions(
            config_version="lending",
            config_revision=7,
            config_source="prefix-mapping",
            config_mapping_prefix="acme/invoices/",
        )
        assert names["#ConfigSource"] == "ConfigSource"
        assert values[":ConfigSource"] == "prefix-mapping"
        assert names["#ConfigMappingPrefix"] == "ConfigMappingPrefix"
        assert values[":ConfigMappingPrefix"] == "acme/invoices/"

    def test_the_assignment_error_is_persisted(self):
        _expr, names, values = self._expressions(
            config_assignment_error="Refused: because."
        )
        assert names["#ConfigAssignmentError"] == "ConfigAssignmentError"
        assert values[":ConfigAssignmentError"] == "Refused: because."

    def test_a_mapping_that_lost_still_records_its_prefix(self):
        """ "A mapping was consulted and deferred" is a different fact from "no
        mapping matched", and only the first explains a surprising profile."""
        _expr, _names, values = self._expressions(
            config_version="chosen",
            config_source="metadata",
            config_mapping_prefix="acme/",
        )
        assert values[":ConfigSource"] == "metadata"
        assert values[":ConfigMappingPrefix"] == "acme/"

    def test_absent_when_unknown(self):
        _expr, _names, values = self._expressions(config_version="lending")
        assert ":ConfigSource" not in values
        assert ":ConfigMappingPrefix" not in values
        assert ":ConfigAssignmentError" not in values


@pytest.mark.unit
class TestTheReadBack:
    def setup_method(self):
        self.service = DocumentDynamoDBService(dynamodb_client=Mock())

    def test_provenance_round_trips_from_a_tracking_item(self):
        doc = self.service._dynamodb_item_to_document(
            {
                "ObjectKey": "test.pdf",
                "ObjectStatus": "FAILED",
                "ConfigVersion": "regulated",
                "ConfigRevision": Decimal("3"),
                "ConfigSource": "rejected",
                "ConfigMappingPrefix": "regulated/",
                "ConfigAssignmentError": "Refused: because.",
            }
        )
        assert doc.config_version == "regulated"
        assert doc.config_revision == 3
        assert doc.config_source == "rejected"
        assert doc.config_mapping_prefix == "regulated/"
        assert doc.config_assignment_error == "Refused: because."

    def test_an_old_row_with_no_provenance_still_reads(self):
        """Backward compatibility: every document written before this existed."""
        doc = self.service._dynamodb_item_to_document(
            {
                "ObjectKey": "old.pdf",
                "ObjectStatus": "COMPLETED",
                "ConfigVersion": "lending",
            }
        )
        assert doc.config_version == "lending"
        assert doc.config_source is None
        assert doc.config_mapping_prefix is None
        assert doc.config_assignment_error is None


@pytest.mark.unit
class TestTheRunItem:
    """Version history records the configuration each run used.

    A run item is what the version viewer and the run comparison read, so a run
    recorded without its provenance can say which configuration produced it but not
    why that configuration applied -- which is the question when two runs of one
    document disagree.
    """

    def test_provenance_is_recorded_on_a_run_item(self):
        client = Mock()
        service = DocumentDynamoDBService(dynamodb_client=client)
        doc = Document(
            id="test.pdf",
            input_key="test.pdf",
            config_version="lending",
            config_revision=7,
            config_source="prefix-mapping",
            config_mapping_prefix="acme/",
        )
        service.create_document_run(doc, "run-1", "s3://bucket/manifest.json", 1)
        item = client.put_item.call_args.args[0]
        assert item["ConfigVersion"] == "lending"
        assert item["ConfigRevision"] == 7
        assert item["ConfigSource"] == "prefix-mapping"
        assert item["ConfigMappingPrefix"] == "acme/"
