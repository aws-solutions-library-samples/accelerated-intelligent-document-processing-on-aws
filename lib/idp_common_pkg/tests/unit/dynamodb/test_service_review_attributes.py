# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Review-queue writes on the tracking table (IDP-0002).

``update_review_attributes`` is the targeted ``UpdateItem`` the review queue uses
so it never races the whole-document ``update_document``; ``create_document_run``
tags the two runs of a review-queue document with ``RunKind``; and the service's
``calculate_ttl`` delegates to the shared UTC helper.
"""

from datetime import datetime, timezone
from unittest.mock import Mock, patch

import pytest

from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.dynamodb.ttl import calculate_ttl
from idp_common.models import Document, Status


@pytest.mark.unit
class TestUpdateReviewAttributes:
    def setup_method(self):
        self.client = Mock()
        self.service = DocumentDynamoDBService(dynamodb_client=self.client)

    def _call(self):
        self.client.update_item.assert_called_once()
        return self.client.update_item.call_args.kwargs

    def test_set_and_remove_in_one_conditional_update(self):
        self.service.update_review_attributes(
            "batch/a.pdf",
            {"ReviewStatus": "Claimed", "ReviewOwner": "alice"},
            remove_attrs=["ReviewNote"],
        )
        kwargs = self._call()
        assert kwargs["key"] == {"PK": "doc#batch/a.pdf", "SK": "none"}
        assert kwargs["update_expression"] == ("SET #s0 = :s0, #s1 = :s1 REMOVE #r0")
        assert kwargs["expression_attribute_names"] == {
            "#s0": "ReviewStatus",
            "#s1": "ReviewOwner",
            "#r0": "ReviewNote",
        }
        assert kwargs["expression_attribute_values"] == {
            ":s0": "Claimed",
            ":s1": "alice",
        }
        # The row must already exist: a review write never creates a tracking row.
        assert kwargs["condition_expression"] == "attribute_exists(PK)"
        assert kwargs["return_values"] == "NONE"

    def test_remove_only_sends_no_attribute_values(self):
        # DynamoDB rejects an empty ExpressionAttributeValues map, so it must be None.
        self.service.update_review_attributes("a.pdf", {}, remove_attrs=["ReviewOwner"])
        kwargs = self._call()
        assert kwargs["update_expression"] == "REMOVE #r0"
        assert kwargs["expression_attribute_names"] == {"#r0": "ReviewOwner"}
        assert kwargs["expression_attribute_values"] is None

    def test_an_attribute_both_set_and_removed_is_only_set(self):
        # SET and REMOVE of the same path in one expression is a ValidationException.
        self.service.update_review_attributes(
            "a.pdf",
            {"ReviewOwner": "bob"},
            remove_attrs=["ReviewOwner", "ReviewNote"],
        )
        kwargs = self._call()
        assert kwargs["update_expression"] == "SET #s0 = :s0 REMOVE #r1"
        assert kwargs["expression_attribute_names"] == {
            "#s0": "ReviewOwner",
            "#r1": "ReviewNote",
        }

    def test_nothing_to_write_makes_no_call(self):
        self.service.update_review_attributes("a.pdf", {}, remove_attrs=())
        self.client.update_item.assert_not_called()

    def test_removing_only_attributes_that_are_also_set_still_writes_the_set(self):
        self.service.update_review_attributes(
            "a.pdf", {"ReviewStatus": "Done"}, remove_attrs=["ReviewStatus"]
        )
        assert self._call()["update_expression"] == "SET #s0 = :s0"


@pytest.mark.unit
class TestRunKind:
    def setup_method(self):
        self.client = Mock()
        self.service = DocumentDynamoDBService(dynamodb_client=self.client)
        self.doc = Document(id="a.pdf", input_key="a.pdf", status=Status.COMPLETED)

    def _run_item(self, **kwargs):
        self.service.create_document_run(
            self.doc, run_id="r1", manifest_uri="s3://out/m.json", **kwargs
        )
        return self.client.put_item.call_args[0][0]

    def test_run_kind_is_recorded_when_given(self):
        assert self._run_item(run_kind="review_phase1")["RunKind"] == "review_phase1"

    def test_run_kind_is_absent_for_an_ordinary_run(self):
        assert "RunKind" not in self._run_item()

    def test_run_kind_is_keyword_only(self):
        with pytest.raises(TypeError):
            self.service.create_document_run(
                self.doc, "r1", "s3://out/m.json", 0, None, "review_final"
            )


@pytest.mark.unit
class TestServiceCalculateTtl:
    def test_delegates_to_the_shared_utc_helper(self):
        service = DocumentDynamoDBService(dynamodb_client=Mock())
        with patch(
            "idp_common.dynamodb.service.calculate_ttl", return_value=42
        ) as helper:
            assert service.calculate_ttl(7) == 42
        helper.assert_called_once_with(7)

    def test_is_days_from_now_in_epoch_seconds(self):
        service = DocumentDynamoDBService(dynamodb_client=Mock())
        before = calculate_ttl(30, now=datetime.now(timezone.utc))
        assert abs(service.calculate_ttl() - before) <= 2
