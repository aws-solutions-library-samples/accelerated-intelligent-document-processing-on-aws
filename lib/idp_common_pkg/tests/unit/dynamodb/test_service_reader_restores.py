# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the tracking-row reader restores for runs that start from a reloaded row.

A run that starts after document assembly (a completed review, a reprocess from a
later step) loads the document from its tracking row and writes it back. Three
things on the row were not read back, and each made such a run wrong:

* ``SubmissionSource`` and ``TestSetId``. Without them a Test Studio document
  looked like a production document, so review routing could hold it.
* ``ConfidenceAlertCount``. Every whole-document write persists the count, so a
  reloaded document stamped the stored figure back to 0.
* ``ConfidenceThresholdAlertsTotal``. The row stores a capped subset of a
  section's alerts and the true figure beside it; writing the reloaded section
  back replaced the figure with the subset's length.
"""

from typing import Any
from unittest.mock import Mock

import pytest

from idp_common.dynamodb.service import (
    _MAX_STORED_ALERTS_PER_SECTION,
    DocumentDynamoDBService,
    set_section_alerts,
)
from idp_common.models import Document, Section
from idp_common.review_queues.routing import is_test_studio_document


def _item(**attributes: Any) -> dict:
    item = {
        "PK": "doc#packet.pdf",
        "SK": "none",
        "ObjectKey": "packet.pdf",
        "ObjectStatus": "COMPLETED",
    }
    item.update(attributes)
    return item


def _stored_alert(index: int) -> dict:
    return {
        "attributeName": f"Transactions[{index}].Amount",
        "confidence": "0.4",
        "confidenceThreshold": "0.9",
    }


def _capped_section_row(total: int) -> dict:
    return {
        "Id": "1",
        "PageIds": [1],
        "Class": "Statement",
        "OutputJSONUri": "s3://out/packet.pdf/sections/1/result.json",
        "ConfidenceThresholdAlerts": [
            _stored_alert(i) for i in range(_MAX_STORED_ALERTS_PER_SECTION)
        ],
        "ConfidenceThresholdAlertsTotal": total,
    }


@pytest.mark.unit
class TestTheReaderRestoresWhatALaterStartNeeds:
    def setup_method(self):
        self.service = DocumentDynamoDBService(dynamodb_client=Mock())

    def test_a_test_studio_row_reads_back_as_a_test_studio_document(self):
        doc = self.service._dynamodb_item_to_document(
            _item(SubmissionSource="test-studio", TestSetId="set-7")
        )

        assert doc.submission_source == "test-studio"
        assert doc.test_set_id == "set-7"
        assert is_test_studio_document(doc) is True

    def test_a_test_set_id_alone_marks_the_document(self):
        """The reader restores each marker on its own, not only the pair."""
        doc = self.service._dynamodb_item_to_document(_item(TestSetId="set-7"))

        assert doc.submission_source is None
        assert doc.test_set_id == "set-7"
        assert is_test_studio_document(doc) is True

    def test_a_production_row_has_no_markers(self):
        doc = self.service._dynamodb_item_to_document(_item())

        assert doc.submission_source is None
        assert doc.test_set_id is None
        assert is_test_studio_document(doc) is False

    def test_the_alert_count_survives_a_load_then_write(self):
        doc = self.service._dynamodb_item_to_document(_item(ConfidenceAlertCount=7))
        assert doc.confidence_alert_count == 7

        _, _, values = self.service._document_to_update_expressions(doc)

        assert values[":ConfidenceAlertCount"] == 7

    def test_a_row_with_no_alert_count_reads_back_as_zero(self):
        doc = self.service._dynamodb_item_to_document(_item())

        assert doc.confidence_alert_count == 0

    def test_a_capped_section_reads_back_with_its_true_total(self):
        doc = self.service._dynamodb_item_to_document(
            _item(Sections=[_capped_section_row(412)])
        )

        (section,) = doc.sections
        assert (
            len(section.confidence_threshold_alerts) == _MAX_STORED_ALERTS_PER_SECTION
        )
        assert section.confidence_alert_total == 412

    def test_the_true_total_survives_a_load_then_write(self):
        doc = self.service._dynamodb_item_to_document(
            _item(Sections=[_capped_section_row(412)])
        )

        _, _, values = self.service._document_to_update_expressions(doc)

        (written,) = values[":Sections"]
        assert (
            len(written["ConfidenceThresholdAlerts"]) == _MAX_STORED_ALERTS_PER_SECTION
        )
        assert written["ConfidenceThresholdAlertsTotal"] == 412

    def test_an_uncapped_section_has_no_total(self):
        row = _capped_section_row(0)
        del row["ConfidenceThresholdAlertsTotal"]
        row["ConfidenceThresholdAlerts"] = row["ConfidenceThresholdAlerts"][:3]

        doc = self.service._dynamodb_item_to_document(_item(Sections=[row]))

        (section,) = doc.sections
        assert section.confidence_alert_total is None
        section_data: dict[str, Any] = {}
        set_section_alerts(section_data, section)
        assert len(section_data["ConfidenceThresholdAlerts"]) == 3
        assert "ConfidenceThresholdAlertsTotal" not in section_data


@pytest.mark.unit
class TestTheTotalWrittenWithASection:
    def test_a_total_with_an_empty_list_is_still_written(self):
        section = Section(section_id="1", classification="Statement")
        section.confidence_alert_total = 12
        section_data: dict[str, Any] = {}

        set_section_alerts(section_data, section)

        assert section_data == {"ConfidenceThresholdAlertsTotal": 12}

    def test_a_list_longer_than_the_carried_total_wins(self):
        """A stale total must not under-report a list that has since grown."""
        section = Section(
            section_id="1",
            classification="Statement",
            confidence_threshold_alerts=[
                {"attribute_name": f"f{i}", "confidence": 0.1} for i in range(80)
            ],
        )
        section.confidence_alert_total = 60
        section_data: dict[str, Any] = {}

        set_section_alerts(section_data, section)

        assert section_data["ConfidenceThresholdAlertsTotal"] == 80

    def test_a_section_with_neither_writes_nothing(self):
        section_data: dict[str, Any] = {}

        set_section_alerts(section_data, Section(section_id="1", classification="X"))

        assert section_data == {}

    def test_the_total_travels_through_the_document_payload(self):
        """The state machine carries the document as a dict between steps."""
        document = Document(id="packet.pdf", input_key="packet.pdf")
        section = Section(section_id="1", classification="Statement")
        section.confidence_alert_total = 412
        document.sections.append(section)
        document.sections.append(Section(section_id="2", classification="Cover"))

        payload = document.to_dict()
        restored = Document.from_dict(payload)

        assert payload["sections"][0]["confidence_alert_total"] == 412
        assert "confidence_alert_total" not in payload["sections"][1]
        assert restored.sections[0].confidence_alert_total == 412
        assert restored.sections[1].confidence_alert_total is None
        assert Section.from_dict(section.to_dict()).confidence_alert_total == 412
        assert (
            "confidence_alert_total"
            not in Section(section_id="2", classification="Cover").to_dict()
        )


@pytest.mark.unit
class TestPolicyRuleValidationResult:
    """The result is stored as a JSON string and restored only on request.

    A run that requeues a document read from its row (saved edits, a completed
    review) must validate the policy rules again, and the stage skips a document
    that already carries a result. So a plain read leaves the result off, as it
    always has. A run that starts after the stage asks for it.
    """

    def _stored(self) -> dict:
        from idp_common.models import RuleValidationResult, Status

        service = DocumentDynamoDBService(dynamodb_client=Mock())
        document = Document(id="packet.pdf", input_key="packet.pdf")
        document.status = Status.COMPLETED
        document.rule_validation_result = RuleValidationResult(
            request_id="r-1",
            output_uri="s3://out/packet.pdf/rule_validation/consolidated/summary.md",
            matched_policy_types=["lending"],
            matched_page_ids={"lending": ["1"]},
        )
        _, _, values = service._document_to_update_expressions(document)
        stored = values[":RuleValidationResult"]
        assert isinstance(stored, str)
        return _item(RuleValidationResult=stored)

    def _service(self, item) -> DocumentDynamoDBService:
        client = Mock()
        client.get_item.return_value = item
        return DocumentDynamoDBService(dynamodb_client=client)

    def test_a_plain_read_leaves_the_result_off(self):
        document = self._service(self._stored()).get_document("packet.pdf")

        assert document.rule_validation_result is None

    def test_a_read_that_asks_for_it_restores_it(self):
        document = self._service(self._stored()).get_document(
            "packet.pdf", with_policy_rule_validation=True
        )

        result = document.rule_validation_result
        assert result.request_id == "r-1"
        assert result.output_uri.endswith("consolidated/summary.md")
        assert result.matched_policy_types == ["lending"]
        assert result.matched_page_ids == {"lending": ["1"]}

    def test_a_row_with_no_result_has_none_either_way(self):
        service = self._service(_item())

        assert service.get_document("packet.pdf").rule_validation_result is None
        assert (
            service.get_document(
                "packet.pdf", with_policy_rule_validation=True
            ).rule_validation_result
            is None
        )

    def test_a_stored_value_that_is_not_json_is_left_off(self):
        service = self._service(_item(RuleValidationResult="{not json"))

        document = service.get_document("packet.pdf", with_policy_rule_validation=True)

        assert document.rule_validation_result is None
        assert document.input_key == "packet.pdf"

    def test_a_result_stored_as_a_map_is_restored_by_any_read(self):
        """The form an earlier writer stored; it has always been read."""
        service = self._service(
            _item(
                RuleValidationResult={"request_id": "r-0", "output_uri": "s3://o/s.md"}
            )
        )

        assert service.get_document("packet.pdf").rule_validation_result.request_id == (
            "r-0"
        )
