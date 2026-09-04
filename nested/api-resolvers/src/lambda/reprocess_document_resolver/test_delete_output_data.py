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
            # index.py builds boto3 clients at import, which need a region. Without
            # this the tests inherit one from the developer's environment (or
            # ~/.aws/config) and pass locally, then fail in CI with NoRegionError —
            # the same trap 79de4f1d3 fixed for queue_sender's tests. Reproduce a
            # CI-like environment with:
            #   env -u AWS_DEFAULT_REGION -u AWS_REGION -u AWS_PROFILE \
            #     AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null \
            #     pytest test_delete_output_data.py
            "AWS_DEFAULT_REGION": "us-east-1",
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

        with (
            patch.object(index, "cloudwatch"),
            patch.object(
                index,
                "delete_current_output_objects",
                side_effect=RuntimeError("simulated S3 outage"),
            ),
        ):
            # Must not raise.
            index._delete_output_data("doc.pdf")

    def test_failure_emits_alarmable_metric(self):
        """Parity with queue_sender: on purge failure the reprocess
        resolver must emit ``StaleOutputPurgeFailed`` so an operator
        can alarm on it without depending on log-scraping."""
        import index

        with (
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(
                index,
                "delete_current_output_objects",
                side_effect=RuntimeError("partial-purge S3 error"),
            ),
        ):
            index._delete_output_data("doc.pdf")

        mock_cw.put_metric_data.assert_called_once()
        call = mock_cw.put_metric_data.call_args
        # Lock the exact Namespace — the code's fallback ``IDP`` in test
        # env; a refactor typo (``idp``, ``IDP-Test``, ...) fails here
        # rather than silently drifting.
        assert call.kwargs["Namespace"] == "IDP"
        metrics = call.kwargs["MetricData"]
        assert metrics[0]["MetricName"] == "StaleOutputPurgeFailed"
        assert metrics[0]["Value"] == 1
        assert metrics[0]["Unit"] == "Count"

    def test_metric_namespace_is_read_from_module_attribute(self):
        """Regression guard against an env-var rename slipping through:
        the module-level ``METRIC_NAMESPACE`` binds at import time
        (before the autouse ``_env`` fixture runs), so
        ``test_failure_emits_alarmable_metric`` only ever exercises the
        ``os.environ.get(..., 'IDP')`` fallback branch. A future rename
        of the env var would silently keep hitting the fallback in
        prod, emitting under the wrong namespace and the alarm never
        fires. Patching the module attribute directly to a stack-name-
        shaped value asserts the emit uses the value ``METRIC_NAMESPACE``
        actually resolves to at runtime — so the template's
        ``METRIC_NAMESPACE: !Ref StackName`` wiring is exercised by the
        assertion path."""
        import index

        with (
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(index, "METRIC_NAMESPACE", "idp-dev-qs"),
            patch.object(
                index,
                "delete_current_output_objects",
                side_effect=RuntimeError("purge failed"),
            ),
        ):
            index._delete_output_data("doc.pdf")

        assert mock_cw.put_metric_data.call_args.kwargs["Namespace"] == "idp-dev-qs"

    def test_metric_emit_failure_is_swallowed(self):
        """Telemetry must not affect the reprocess flow — if
        PutMetricData itself throws (throttled / network), the outer
        try/except swallows it and _delete_output_data returns
        normally."""
        import index

        with (
            patch.object(index, "cloudwatch") as mock_cw,
            patch.object(
                index,
                "delete_current_output_objects",
                side_effect=RuntimeError("purge failed"),
            ),
        ):
            mock_cw.put_metric_data.side_effect = RuntimeError("CW throttle")
            # Must not raise.
            index._delete_output_data("doc.pdf")
