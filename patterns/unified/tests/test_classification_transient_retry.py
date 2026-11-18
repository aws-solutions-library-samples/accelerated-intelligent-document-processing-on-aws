# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The classification handler reports a transient failure as ``TransientError`` (#2034).

``ClassificationStep`` retries ``TransientError`` beside the modeled Bedrock codes
(``test_workflow_transient_retry.py`` holds the state machine half). This is the
handler half: a transient cause leaves the handler under that one name, whether the
classification service raised it, raised the raw botocore error, or stored it as the
page-level path's ``primary_exception``. A deterministic failure keeps its own name, so
it is not retried.

Driven through the real handler with the document service, configuration, metering
and serialization stubbed, the same way ``test_classification_forced_grouping.py``
drives it.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from idp_common.models import Document, Page, Status
from idp_common.utils.transient_errors import TransientError

_HANDLER = Path(__file__).resolve().parents[1] / "src/classification_function/index.py"
# Named as one literal so test_handler_imports_are_region_free.py, which derives the
# handlers it checks from literal paths in test files, covers this handler as well.
_LITERAL_PATH_FOR_DISCOVERY = "patterns/unified/src/classification_function/index.py"

pytestmark = pytest.mark.unit

#: botocore builds a class of this name for a modeled Bedrock throttle.
ThrottlingException = type("ThrottlingException", (ClientError,), {})


def _load():
    """Import the handler with X-Ray disabled and its global patching suppressed."""
    with (
        patch.dict(os.environ, {"AWS_XRAY_SDK_ENABLED": "false"}),
        patch("aws_xray_sdk.core.patch_all"),
    ):
        if str(_HANDLER.parent) not in sys.path:
            sys.path.insert(0, str(_HANDLER.parent))
        spec = importlib.util.spec_from_file_location(
            "classification_function_transient_retry", _HANDLER
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


@pytest.fixture(scope="module")
def handler_module():
    return _load()


def _document() -> Document:
    doc = Document(
        id="run1/packet.pdf",
        input_key="run1/packet.pdf",
        output_bucket="out",
        status=Status.OCR,
    )
    doc.pages = {str(n): Page(page_id=str(n)) for n in (1, 2)}
    return doc


def _invoke(module, classify):
    """Run the handler with ``classify`` standing in for ``classify_document``."""
    service = MagicMock()
    service.return_value.classify_document.side_effect = classify
    with (
        patch.dict(os.environ, {"AWS_XRAY_SDK_ENABLED": "false"}),
        patch.object(module.Document, "load_document", return_value=_document()),
        patch.object(module, "get_config", return_value=MagicMock(model_dump=dict)),
        patch.object(module, "create_document_service"),
        patch.object(module, "calculate_lambda_metering", return_value={}),
        patch.object(module, "metrics"),
        patch.object(module.classification, "ClassificationService", service),
        patch.object(Document, "serialize_document", return_value={"serialized": 1}),
    ):
        return module.handler(
            {"OCRResult": {"document": {}}, "execution_arn": "a"}, None
        )


def _raises(exc):
    def classify(document):
        raise exc

    return classify


class TestTheHandlerReportsTransientFailuresByOneName:
    def test_a_modeled_throttle_is_reported_as_transient_error(self, handler_module):
        throttle = ThrottlingException(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "Converse",
        )
        with pytest.raises(TransientError) as caught:
            _invoke(handler_module, _raises(throttle))
        assert caught.value.__cause__ is throttle

    def test_a_read_timeout_is_reported_as_transient_error(self, handler_module):
        """Retried by no name ClassificationStep listed before #2034."""
        timeout = ReadTimeoutError(endpoint_url="https://bedrock-runtime")
        with pytest.raises(TransientError) as caught:
            _invoke(handler_module, _raises(timeout))
        assert caught.value.__cause__ is timeout

    def test_the_services_own_transient_error_is_not_wrapped_again(
        self, handler_module
    ):
        raised = TransientError(ReadTimeoutError(endpoint_url="x"), where="segment 2")
        with pytest.raises(TransientError) as caught:
            _invoke(handler_module, _raises(raised))
        assert caught.value is raised

    def test_a_deterministic_failure_keeps_its_own_name(self, handler_module):
        invalid = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "bad input"}},
            "Converse",
        )
        with pytest.raises(ClientError) as caught:
            _invoke(handler_module, _raises(invalid))
        assert caught.value is invalid

    def test_a_page_level_primary_exception_is_reported_as_transient_error(
        self, handler_module
    ):
        """The page-level path stores the first failed page's exception instead of
        raising it, and the handler raises it after updating the document."""
        timeout = ReadTimeoutError(endpoint_url="https://bedrock-runtime")

        def classify(document):
            document.metadata = {
                "failed_page_exceptions": {
                    "2": {
                        "exception_type": "ReadTimeoutError",
                        "exception_message": str(timeout),
                    }
                },
                "primary_exception": timeout,
            }
            return document

        with pytest.raises(TransientError) as caught:
            _invoke(handler_module, classify)
        assert caught.value.__cause__ is timeout
