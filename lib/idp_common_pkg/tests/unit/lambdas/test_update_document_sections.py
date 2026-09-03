# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Re-grouping a processed document's pages without reprocessing it.

The document-side counterpart to ``updateTestSetDocumentSections``, and the reason it
is not simply ``processChanges``: that operation clears each changed section's
extraction data and requeues the document, because it *delegates persistence to the
pipeline* — it never writes the document back, so the SQS run is the only thing that
saves the change. Reprocessing is its persistence mechanism, not something re-grouping
requires.

A grouping-only edit persists itself, as ``complete_section_review`` already does for
field edits, so the extracted values and any HITL corrections to them survive. These
tests pin that, and the one hazard a direct write introduces: racing a run that is
already in flight.
"""

import importlib.util
import os
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "../../../../..")

_ENV = {
    "AWS_REGION": "us-east-1",
    "INPUT_BUCKET": "input-bucket",
    "OUTPUT_BUCKET": "output-bucket",
    "QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/1/q",
    "WORKING_BUCKET": "working-bucket",
}


@pytest.fixture
def resolver():
    """Load the flat Lambda `index.py` by path, with boto3 stubbed at import."""
    with patch.dict(os.environ, _ENV):
        with patch("boto3.client"), patch("boto3.resource"):
            spec = importlib.util.spec_from_file_location(
                "process_changes_resolver_index",
                os.path.join(
                    _REPO_ROOT,
                    "nested/api-resolvers/src/lambda/process_changes_resolver/index.py",
                ),
            )
            if spec is None or spec.loader is None:
                raise ImportError("Could not load process_changes_resolver/index.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod


def _section(module, section_id, page_ids, classification, with_extraction=True):
    section = module.Section(
        section_id=section_id,
        classification=classification,
        page_ids=[str(p) for p in page_ids],
    )
    if with_extraction:
        # What must survive a re-grouping.
        section.extraction_result_uri = f"s3://out/{section_id}/result.json"
        section.attributes = {"total": f"from-{section_id}"}
    return section


def _document(module, sections, status="COMPLETED"):
    document = MagicMock()
    document.status = status
    document.sections = sections
    document.pages = {
        str(p): MagicMock(classification=None)
        for section in sections
        for p in section.page_ids
    }
    return document


def _event(object_key="packet.pdf", sections=None, groups=("Admin",)):
    return {
        "info": {"fieldName": "updateDocumentSections"},
        "identity": {"claims": {"cognito:groups": list(groups)}},
        "arguments": {"objectKey": object_key, "sections": sections or []},
    }


def _call(module, document, event):
    service = MagicMock()
    service.get_document.return_value = document
    with patch.object(module, "create_document_service", return_value=service):
        result = module.handler(event, None)
    return result, service


@pytest.mark.unit
class TestUpdateDocumentSections:
    def test_regrouping_preserves_extraction_and_does_not_requeue(self, resolver):
        """The property the whole operation exists for.

        processChanges would clear these values and requeue; a reviewer who corrected
        forty fields and then noticed the split was wrong would lose the forty.
        """
        document = _document(
            resolver,
            [
                _section(resolver, "1", [1, 2], "FieldTicket"),
                _section(resolver, "2", [3, 4], "Invoice"),
            ],
        )
        event = _event(
            sections=[
                {
                    "sectionId": "1",
                    "classification": "FieldTicket",
                    "pageIds": ["1", "2", "3"],
                },
                {"sectionId": "2", "classification": "Invoice", "pageIds": ["4"]},
            ]
        )

        result, service = _call(resolver, document, event)

        assert result["success"] is True
        assert [s.page_ids for s in document.sections] == [["1", "2", "3"], ["4"]]
        # Values kept, and attached to the section that carried them.
        assert document.sections[0].attributes == {"total": "from-1"}
        assert document.sections[1].attributes == {"total": "from-2"}
        assert document.sections[0].extraction_result_uri is not None
        # Persisted directly, not handed to the pipeline.
        service.update_document.assert_called_once_with(document)
        assert result["processingJobId"] is None

    def test_status_is_untouched_so_the_document_is_not_reprocessed(self, resolver):
        document = _document(resolver, [_section(resolver, "1", [1, 2], "Invoice")])
        event = _event(
            sections=[
                {"sectionId": "1", "classification": "Invoice", "pageIds": ["1", "2"]}
            ]
        )

        _call(resolver, document, event)

        # processChanges sets this to QUEUED and sends to SQS; that is exactly what this
        # operation must not do.
        assert document.status == "COMPLETED"

    def test_sections_are_ordered_by_first_page(self, resolver):
        """Consumers take a group index from list position, so order has to follow the
        document — same rule processChanges applies."""
        document = _document(
            resolver,
            [
                _section(resolver, "1", [3, 4], "FieldTicket"),
                _section(resolver, "2", [1, 2], "Invoice"),
            ],
        )
        event = _event(
            sections=[
                {
                    "sectionId": "1",
                    "classification": "FieldTicket",
                    "pageIds": ["3", "4"],
                },
                {"sectionId": "2", "classification": "Invoice", "pageIds": ["1", "2"]},
            ]
        )

        _call(resolver, document, event)

        assert [s.section_id for s in document.sections] == ["2", "1"]

    def test_page_classification_follows_its_new_section(self, resolver):
        """Otherwise a moved page keeps a class its section no longer has."""
        document = _document(
            resolver,
            [
                _section(resolver, "1", [1, 2], "FieldTicket"),
                _section(resolver, "2", [3], "Invoice"),
            ],
        )
        event = _event(
            sections=[
                {"sectionId": "1", "classification": "FieldTicket", "pageIds": ["1"]},
                {"sectionId": "2", "classification": "Invoice", "pageIds": ["2", "3"]},
            ]
        )

        _call(resolver, document, event)

        assert document.pages["2"].classification == "Invoice"
        assert document.pages["1"].classification == "FieldTicket"

    def test_a_new_section_carries_no_extraction_result(self, resolver):
        """Nothing has run over these pages as a group, and saying so beats inventing
        a copy of the original's values."""
        document = _document(resolver, [_section(resolver, "1", [1, 2, 3], "Invoice")])
        event = _event(
            sections=[
                {"sectionId": "1", "classification": "Invoice", "pageIds": ["1"]},
                {"sectionId": "9", "classification": "W2", "pageIds": ["2", "3"]},
            ]
        )

        _call(resolver, document, event)

        added = next(s for s in document.sections if s.section_id == "9")
        assert getattr(added, "extraction_result_uri", None) is None
        assert added.classification == "W2"

    def test_every_non_terminal_status_is_refused(self, resolver):
        """The one hazard a direct write introduces.

        processChanges is safe to call mid-run because it never writes — it hands the
        document to SQS. This would race the pipeline's own save.

        Enumerated from the Status enum rather than spot-checked. The first version of
        this test looped over QUEUED and RUNNING only, matching a guard that listed the
        same two — so the 14 statuses the pipeline actually spends its time in (OCR,
        CLASSIFYING, EXTRACTING, ASSESSING, SUMMARIZING, RULE_VALIDATION*, …) passed
        the check and got a read-modify-write underneath the running workflow. Driving
        the test off the enum means a new pipeline stage cannot reopen the hole
        unnoticed: it fails here until it is classified as terminal or not.
        """
        from idp_common.models import Status

        terminal = {
            Status.COMPLETED,
            Status.FAILED,
            Status.ABORTED,
            Status.REDACTED_SUPERSEDED,
        }
        non_terminal = [s for s in Status if s not in terminal]
        assert len(non_terminal) >= 14, "enum shrank; re-check the allow-list"

        for status in non_terminal:
            document = _document(
                resolver,
                [_section(resolver, "1", [1], "Invoice")],
                status=status.value,
            )
            event = _event(sections=[{"sectionId": "1", "pageIds": ["1"]}])

            result, service = _call(resolver, document, event)

            assert result["success"] is False, f"{status.value} was allowed through"
            service.update_document.assert_not_called()

    def test_every_terminal_status_is_allowed(self, resolver):
        """The other half: failing closed must not refuse a document nothing is writing.

        A reviewer fixing the split on a FAILED or ABORTED document is a legitimate
        thing to do, and REDACTED_SUPERSEDED is terminal too — a preprocessing hook
        made a redacted copy that processes separately and this original is left alone.
        """
        from idp_common.models import Status

        for status in (
            Status.COMPLETED,
            Status.FAILED,
            Status.ABORTED,
            Status.REDACTED_SUPERSEDED,
        ):
            document = _document(
                resolver,
                [_section(resolver, "1", [1], "Invoice")],
                status=status.value,
            )
            event = _event(sections=[{"sectionId": "1", "pageIds": ["1"]}])

            result, service = _call(resolver, document, event)

            assert result["success"] is True, f"{status.value} was refused"
            service.update_document.assert_called_once_with(document)

    def test_a_repeated_section_id_is_refused(self, resolver):
        """Otherwise it corrupts silently instead of failing.

        The rebuild looks each id up in existing_by_id, so two entries sharing one id
        resolve to the same Section object: it is appended twice and the second
        assignment overwrites page_ids, leaving the document holding two references to
        one section with the first group's pages in none. The page-level checks pass —
        both groups' pages are accounted for — so nothing else catches it.
        """
        document = _document(
            resolver,
            [
                _section(resolver, "1", [1, 2], "Invoice"),
                _section(resolver, "2", [3], "W2"),
            ],
        )
        event = _event(
            sections=[
                {"sectionId": "1", "pageIds": ["1"]},
                {"sectionId": "1", "pageIds": ["2"]},
                {"sectionId": "2", "pageIds": ["3"]},
            ]
        )

        result, service = _call(resolver, document, event)

        assert result["success"] is False
        assert "more than once" in result["message"]
        service.update_document.assert_not_called()

    def test_a_page_the_document_does_not_have_is_refused(self, resolver):
        """It used to be accepted into the section and then skipped by the
        classification loop, so the grouping claimed a page that does not exist."""
        document = _document(resolver, [_section(resolver, "1", [1, 2], "Invoice")])
        event = _event(sections=[{"sectionId": "1", "pageIds": ["1", "2", "99"]}])

        result, service = _call(resolver, document, event)

        assert result["success"] is False
        assert "99" in result["message"]
        service.update_document.assert_not_called()

    def test_a_non_numeric_page_id_is_refused_by_name(self, resolver):
        """It reached int() in the section sort and surfaced as a generic caught
        error, which tells a reviewer nothing about what to correct."""
        document = _document(resolver, [_section(resolver, "1", [1], "Invoice")])
        event = _event(sections=[{"sectionId": "1", "pageIds": ["1", "two"]}])

        result, service = _call(resolver, document, event)

        assert result["success"] is False
        assert "invalid page id" in result["message"]
        service.update_document.assert_not_called()

    def test_a_page_in_two_sections_is_refused(self, resolver):
        document = _document(
            resolver,
            [
                _section(resolver, "1", [1, 2], "Invoice"),
                _section(resolver, "2", [3], "W2"),
            ],
        )
        event = _event(
            sections=[
                {"sectionId": "1", "pageIds": ["1", "2", "3"]},
                {"sectionId": "2", "pageIds": ["3"]},
            ]
        )

        result, service = _call(resolver, document, event)

        assert result["success"] is False
        assert "in both section" in result["message"]
        service.update_document.assert_not_called()

    def test_dropping_a_grouped_page_is_refused(self, resolver):
        document = _document(
            resolver,
            [
                _section(resolver, "1", [1, 2], "Invoice"),
                _section(resolver, "2", [3], "W2"),
            ],
        )
        event = _event(sections=[{"sectionId": "1", "pageIds": ["1", "2"]}])

        result, service = _call(resolver, document, event)

        assert result["success"] is False
        assert "would no longer belong to any section" in result["message"]
        service.update_document.assert_not_called()

    def test_an_empty_section_is_refused(self, resolver):
        document = _document(resolver, [_section(resolver, "1", [1], "Invoice")])
        event = _event(
            sections=[
                {"sectionId": "1", "pageIds": ["1"]},
                {"sectionId": "2", "pageIds": []},
            ]
        )

        result, service = _call(resolver, document, event)

        assert result["success"] is False
        assert "has no pages" in result["message"]
        service.update_document.assert_not_called()


@pytest.mark.unit
class TestPerOperationRbac:
    def test_an_annotator_may_regroup_but_not_run_process_changes(self, resolver):
        """The two operations differ in what they destroy, so they differ in who may
        call them. updateDocumentSections matches completeSectionReview — the sibling
        that edits a section's content — while processChanges stays narrower because it
        regenerates extraction data."""
        assert resolver._OPERATION_GROUPS["updateDocumentSections"] == {
            "Admin",
            "Reviewer",
            "Annotator",
        }
        assert resolver._OPERATION_GROUPS["processChanges"] == {"Admin", "Reviewer"}

    def test_a_group_outside_the_operation_is_refused(self, resolver):
        document = _document(resolver, [_section(resolver, "1", [1], "Invoice")])
        event = _event(
            sections=[{"sectionId": "1", "pageIds": ["1"]}], groups=("Viewer",)
        )

        with pytest.raises(PermissionError, match="updateDocumentSections"):
            _call(resolver, document, event)

    def test_process_changes_still_refuses_an_annotator(self, resolver):
        # Widening the new operation must not widen the destructive one alongside it.
        event = {
            "info": {"fieldName": "processChanges"},
            "identity": {"claims": {"cognito:groups": ["Annotator"]}},
            "arguments": {"objectKey": "packet.pdf", "modifiedSections": []},
        }

        with pytest.raises(PermissionError, match="processChanges"):
            resolver.handler(event, None)

    def test_an_event_with_no_field_name_defaults_to_process_changes_rules(
        self, resolver
    ):
        # Direct invocations and older callers carry no info.fieldName; they must not
        # fall through to the wider group set.
        event = {
            "identity": {"claims": {"cognito:groups": ["Annotator"]}},
            "arguments": {"objectKey": "packet.pdf", "modifiedSections": []},
        }

        with pytest.raises(PermissionError, match="processChanges"):
            resolver.handler(event, None)
