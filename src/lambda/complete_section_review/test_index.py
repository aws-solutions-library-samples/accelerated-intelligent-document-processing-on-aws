# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the complete_section_review Lambda.

Focus: the guard that prevents silently marking a section reviewed when the
caller supplied edited data but the section's output URI cannot be resolved.
Without the guard, the resolver returned success and the UI reset its edit
tracking, so the reviewer's edits were lost behind a success message.
"""

import importlib
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))


def _section(section_id, extraction_result_uri):
    return types.SimpleNamespace(
        section_id=section_id, extraction_result_uri=extraction_result_uri
    )


def _document(sections, pending=None, completed=None):
    return types.SimpleNamespace(
        sections=sections,
        hitl_sections_pending=pending or [],
        hitl_sections_completed=completed or [],
        hitl_status=None,
    )


@pytest.fixture
def mod(monkeypatch):
    """Import the resolver module hermetically (stub idp_common + region/env)."""
    for name in ("idp_common", "idp_common.docs_service", "idp_common.models"):
        sys.modules.setdefault(name, MagicMock())
    if MODULE_DIR not in sys.path:
        sys.path.insert(0, MODULE_DIR)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("TRACKING_TABLE_NAME", "test-tracking")
    monkeypatch.setenv("OUTPUT_BUCKET", "test-output")
    sys.modules.pop("index", None)
    return importlib.import_module("index")


@pytest.mark.unit
def test_raises_when_edited_data_but_section_not_found(mod):
    service = MagicMock()
    service.get_document.return_value = _document(
        sections=[_section("other-section", "s3://bucket/other.json")]
    )
    with patch.object(mod, "create_document_service", return_value=service), patch.object(
        mod, "save_edited_data_to_s3"
    ) as save_mock:
        with pytest.raises(ValueError, match="not found"):
            mod.complete_section_review(
                "doc/key.pdf", "sec-1", edited_data='{"x": 1}', username="rev"
            )
    # Nothing persisted and the section was NOT marked reviewed.
    save_mock.assert_not_called()
    service.update_document.assert_not_called()


@pytest.mark.unit
def test_raises_when_edited_data_but_no_output_uri(mod):
    service = MagicMock()
    service.get_document.return_value = _document(
        sections=[_section("sec-1", None)]
    )
    with patch.object(mod, "create_document_service", return_value=service), patch.object(
        mod, "save_edited_data_to_s3"
    ) as save_mock:
        with pytest.raises(ValueError, match="no output URI"):
            mod.complete_section_review(
                "doc/key.pdf", "sec-1", edited_data='{"x": 1}', username="rev"
            )
    save_mock.assert_not_called()
    service.update_document.assert_not_called()


@pytest.mark.unit
def test_saves_and_completes_when_uri_resolves(mod):
    # Two pending sections; completing one leaves the other pending -> no reprocess.
    service = MagicMock()
    service.get_document.return_value = _document(
        sections=[
            _section("sec-1", "s3://bucket/sec1.json"),
            _section("sec-2", "s3://bucket/sec2.json"),
        ],
        pending=["sec-1", "sec-2"],
        completed=[],
    )
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": {}}
    fake_ddb = MagicMock()
    fake_ddb.Table.return_value = fake_table

    with patch.object(mod, "create_document_service", return_value=service), patch.object(
        mod, "save_edited_data_to_s3"
    ) as save_mock, patch.object(mod, "dynamodb", fake_ddb), patch.object(
        mod, "trigger_reprocessing"
    ) as reproc_mock, patch.object(
        mod, "build_document_response", return_value={"ObjectStatus": "ok"}
    ):
        result = mod.complete_section_review(
            "doc/key.pdf", "sec-1", edited_data='{"x": 1}', username="rev"
        )

    save_mock.assert_called_once_with("s3://bucket/sec1.json", '{"x": 1}')
    service.update_document.assert_called_once()
    reproc_mock.assert_not_called()  # sec-2 still pending
    assert result == {"ObjectStatus": "ok"}


@pytest.mark.unit
def test_no_raise_when_no_edited_data_even_if_uri_missing(mod):
    # Completing a review WITHOUT edits must still work even if this section has
    # no output URI (the guard only applies when edited_data is supplied).
    service = MagicMock()
    service.get_document.return_value = _document(
        sections=[
            _section("sec-1", None),
            _section("sec-2", "s3://bucket/sec2.json"),
        ],
        pending=["sec-1", "sec-2"],
        completed=[],
    )
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": {}}
    fake_ddb = MagicMock()
    fake_ddb.Table.return_value = fake_table

    with patch.object(mod, "create_document_service", return_value=service), patch.object(
        mod, "save_edited_data_to_s3"
    ) as save_mock, patch.object(mod, "dynamodb", fake_ddb), patch.object(
        mod, "trigger_reprocessing"
    ), patch.object(mod, "build_document_response", return_value={"ObjectStatus": "ok"}):
        result = mod.complete_section_review(
            "doc/key.pdf", "sec-1", edited_data=None, username="rev"
        )

    save_mock.assert_not_called()
    service.update_document.assert_called_once()
    assert result == {"ObjectStatus": "ok"}


@pytest.mark.unit
def test_skip_all_triggers_reprocessing(mod):
    """Skipping all reviews must trigger the same downstream reprocessing as
    completing the final section, so both "finish review" paths behave
    identically and the post-processing Lambda hook fires in both cases."""
    service = MagicMock()
    service.get_document.return_value = _document(
        sections=[
            _section("sec-1", "s3://bucket/sec1.json"),
            _section("sec-2", "s3://bucket/sec2.json"),
        ],
        completed=[],
    )
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": {}}
    fake_ddb = MagicMock()
    fake_ddb.Table.return_value = fake_table

    with patch.object(mod, "create_document_service", return_value=service), patch.object(
        mod, "dynamodb", fake_ddb
    ), patch.object(mod, "trigger_reprocessing") as reproc_mock, patch.object(
        mod, "build_document_response", return_value={"ObjectStatus": "ok"}
    ):
        result = mod.skip_all_sections_review(
            "doc/key.pdf",
            username="admin",
            user_email="admin@example.com",
            is_admin=True,
        )

    # The document is finalized in DynamoDB (status set, pending cleared) ...
    fake_table.update_item.assert_called_once()
    # ... and downstream reprocessing/post-processing is triggered — parity with
    # the section-by-section completion path.
    reproc_mock.assert_called_once_with("doc/key.pdf")
    assert result == {"ObjectStatus": "ok"}


@pytest.mark.unit
def test_skip_all_finalizes_status_review_skipped(mod):
    """Skip-all sets HITLStatus='Review Skipped' and HITLCompleted=True even
    while (now) also triggering reprocessing."""
    service = MagicMock()
    service.get_document.return_value = _document(
        sections=[_section("sec-1", "s3://bucket/sec1.json")],
        completed=[],
    )
    fake_table = MagicMock()
    fake_table.get_item.return_value = {"Item": {}}
    fake_ddb = MagicMock()
    fake_ddb.Table.return_value = fake_table

    with patch.object(mod, "create_document_service", return_value=service), patch.object(
        mod, "dynamodb", fake_ddb
    ), patch.object(mod, "trigger_reprocessing"), patch.object(
        mod, "build_document_response", return_value={"ObjectStatus": "ok"}
    ):
        mod.skip_all_sections_review("doc/key.pdf", username="admin", is_admin=True)

    _, kwargs = fake_table.update_item.call_args
    values = kwargs["ExpressionAttributeValues"]
    assert values[":status"] == "Review Skipped"
    assert values[":hitlCompleted"] is True
    assert values[":pending"] == []
