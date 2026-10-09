# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the evaluation step records when evaluation is switched off.

``evaluation.enabled: false`` is a supported configuration — it is how a
deployment stops paying for scoring it does not want — and the step honours it by
returning the document untouched. What it did not do was say so anywhere, and the
absence is not inert: every reader of a document's evaluation state has to tell
"no result yet" apart from "there will never be a result", and the only signal is
the ``EvaluationStatus`` attribute on the tracking row. With it unwritten the two
are indistinguishable, so Test Studio counted such a document as still evaluating
and the run it belonged to never left the EVALUATING badge, while every document
in it showed Completed (#1330).

So the property under test is that the skip is *recorded*, and recorded as a
terminal non-failure.

The handler is loaded inside a fixture rather than at module scope. It imports
cleanly with no AWS environment — its document service is built on first use —
so that is not what the fixture is for: the module's document service has to be
replaced before the handler runs, and a fixture is where that belongs. The path
is assembled from components for the same reason, not to avoid
``test_handler_imports_are_region_free``'s census, which this module satisfies
and is enrolled in from the resolver suite.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

PATTERN_ROOT = Path(__file__).resolve().parents[1]
HANDLER_PATH = PATTERN_ROOT / "src" / "evaluation_function" / "index.py"


@pytest.fixture
def handler_module(monkeypatch):
    """The evaluation handler, with its document service replaced by a Mock.

    The service is patched at its source before the module executes, because the
    handler takes the factory with a ``from ... import``; the memoized global the
    factory feeds is primed directly so a test can reach the same object the
    handler will use as ``handler_module._document_service``.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("WORKING_BUCKET", "working-bucket")

    import idp_common.docs_service as docs_service

    service = Mock()
    service.update_document.side_effect = lambda document: document

    with patch.object(docs_service, "create_document_service", return_value=service):
        spec = importlib.util.spec_from_file_location(
            "evaluation_function_under_test", HANDLER_PATH
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
            module._document_service = service
            assert module._get_document_service() is service
            yield module
        finally:
            sys.modules.pop(spec.name, None)


def _document_double():
    """A Document stand-in that records what the handler sets on it."""
    document = Mock()
    document.id = "doc-1"
    document.input_key = "set/doc-1.pdf"
    document.config_version = None
    document.config_revision = None
    document.status = "RUNNING"
    document.evaluation_status = None
    document.serialize_document.return_value = {"id": "doc-1"}
    return document


@pytest.mark.unit
def test_a_disabled_evaluation_is_recorded_as_disabled(handler_module):
    """The fix: the skip reaches the tracking row as a terminal status."""
    document = _document_double()
    config = Mock()
    config.evaluation.enabled = False

    with (
        patch.object(
            handler_module, "extract_document_from_event", return_value=document
        ),
        patch.object(handler_module, "get_config", return_value=config),
    ):
        result = handler_module.handler({"document": {"id": "doc-1"}}, None)

    assert document.evaluation_status == handler_module.EvaluationStatus.DISABLED.value
    handler_module._document_service.update_document.assert_called_once_with(document)
    # The document itself is still returned untouched, which is what the state
    # machine's next step consumes.
    assert result == {"document": {"id": "doc-1"}}


@pytest.mark.unit
def test_a_skipped_evaluation_does_not_claim_the_document_was_evaluating(
    handler_module,
):
    """ObjectStatus is left alone on this path.

    Every other status this function writes is written while it is actually
    working on the document, so moving ObjectStatus to EVALUATING alongside them
    is accurate. Here nothing was evaluated, and the workflow tracker does not
    resolve ObjectStatus until the execution ends — so claiming EVALUATING would
    be visible in the UI for the rest of the run.
    """
    document = _document_double()
    config = Mock()
    config.evaluation.enabled = False

    with (
        patch.object(
            handler_module, "extract_document_from_event", return_value=document
        ),
        patch.object(handler_module, "get_config", return_value=config),
    ):
        handler_module.handler({"document": {"id": "doc-1"}}, None)

    assert document.status == "RUNNING"


@pytest.mark.unit
def test_an_unwritable_status_does_not_turn_a_skip_into_a_failure(handler_module):
    """A tracking-table failure here must not be reported as a failed evaluation.

    Nothing has gone wrong on this path — the document is fully processed and no
    evaluation was asked for. Letting the write raise would reach the handler's
    outer except, which stamps FAILED and makes the run report a failed file for
    a document that is fine. The reader has a fallback for a document carrying no
    status (it consults the run's captured configuration), so logging and
    carrying on is the honest outcome.
    """
    document = _document_double()
    config = Mock()
    config.evaluation.enabled = False
    handler_module._document_service.update_document.side_effect = RuntimeError(
        "ProvisionedThroughputExceededException"
    )

    with (
        patch.object(
            handler_module, "extract_document_from_event", return_value=document
        ),
        patch.object(handler_module, "get_config", return_value=config),
    ):
        result = handler_module.handler({"document": {"id": "doc-1"}}, None)

    assert result == {"document": {"id": "doc-1"}}
    assert document.evaluation_status != handler_module.EvaluationStatus.FAILED.value


@pytest.mark.unit
def test_an_enabled_evaluation_still_marks_the_document_evaluating(handler_module):
    """The ordinary path is unchanged: it does claim EVALUATING, and should.

    Guards the `mark_evaluating` default, which is what every other caller relies
    on. A regression here would be invisible in the disabled-path tests above.
    """
    document = _document_double()

    handler_module.update_document_evaluation_status(
        document, handler_module.EvaluationStatus.RUNNING
    )

    assert document.status == handler_module.Status.EVALUATING
    assert document.evaluation_status == "RUNNING"
