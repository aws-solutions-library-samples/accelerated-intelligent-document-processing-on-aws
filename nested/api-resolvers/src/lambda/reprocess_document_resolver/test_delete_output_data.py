# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit test for reprocess_document_resolver._delete_output_data.

Guards the argument passing into ``delete_current_output_objects``:
reprocess is an admin "start over" action and MUST call the broad-purge
path (``subprefixes=None``). If a future refactor accidentally passes
``subprefixes=("pages/",)`` here, the broad purge intent would silently
narrow — sections/, summary/, evaluation/ would survive across a
reprocess and downstream stages would see stale data.

The queue_sender path (issue #719) uses the narrow ``pages/`` scope
deliberately; the reprocess path must NOT.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Heavy AWS/idp_common deps mocked before import.
sys.modules["idp_common"] = MagicMock()
sys.modules["idp_common.docs_service"] = MagicMock()
sys.modules["idp_common.config_scope"] = MagicMock()
sys.modules["idp_common.document_versions"] = MagicMock()
sys.modules["idp_common.models"] = MagicMock()
sys.modules["idp_common.utils"] = MagicMock()
sys.modules["idp_common.utils.log_sanitizer"] = MagicMock()


@pytest.fixture(autouse=True)
def _env():
    with patch.dict(
        os.environ,
        {
            "OUTPUT_BUCKET": "test-out",
            "INPUT_BUCKET": "test-in",
            "QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/1/test-queue",
            "TRACKING_TABLE": "test-tracking",
            "LOG_LEVEL": "INFO",
        },
    ):
        yield


@pytest.mark.unit
class TestDeleteOutputDataArgPassing:
    def test_calls_helper_with_broad_purge_semantics(self):
        """Reprocess must call the helper with input_key positional and NO
        ``subprefixes`` kwarg, so the default ``None`` (broad purge) fires.
        Explicitly passing ``subprefixes=("pages/",)`` would narrow the
        purge and defeat the reprocess "start over" intent."""
        import index

        with patch.object(index, "delete_current_output_objects") as mock_purge:
            mock_purge.return_value = 3
            index._delete_output_data("doc.pdf")

        assert mock_purge.call_count == 1
        call = mock_purge.call_args
        # Positional: s3_client, output_bucket, input_key.
        assert call.args[1] == "test-out"
        assert call.args[2] == "doc.pdf"
        # Broad purge: subprefixes MUST default to None (not passed).
        assert "subprefixes" not in call.kwargs

    def test_failure_is_non_fatal(self):
        """A purge failure must not raise out of _delete_output_data —
        the caller's happy path still enqueues the reprocess."""
        import index

        with patch.object(
            index,
            "delete_current_output_objects",
            side_effect=RuntimeError("simulated S3 outage"),
        ):
            # Must not raise.
            index._delete_output_data("doc.pdf")
