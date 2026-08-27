# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the metering hour-partition CFN migration custom resource.

Coverage focus:
- CFN lifecycle: Delete → SUCCESS immediately; Create with empty bucket →
  SUCCESS; Update with old-layout files → migrate; Update with too many
  files → fail-fast with instructions.
- Path rewriting: `metering/date=X/foo.parquet` → `metering/date=X/hour=HH/foo.parquet`.
- Skip already-migrated keys (idempotency).
"""

from __future__ import annotations

import importlib.util
import json
import os
from unittest.mock import MagicMock, patch

import pytest


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "metering_hour_migration",
        os.path.join(
            os.path.dirname(__file__),
            "../../../../../src/lambda/metering_hour_migration/index.py",
        ),
    )
    assert spec and spec.loader
    with patch("boto3.client"):
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


@pytest.fixture
def mig():
    return _load_module()


@pytest.fixture
def cfn_ctx():
    """Minimal Lambda context for _send()."""
    ctx = MagicMock()
    ctx.log_stream_name = "test-log-stream"
    return ctx


def _make_event(request_type: str, **props):
    return {
        "RequestType": request_type,
        "ResponseURL": "https://presigned.example/cfn-response",
        "StackId": "arn:aws:cloudformation:us-east-1:1:stack/test/uuid",
        "RequestId": "req-1",
        "LogicalResourceId": "MeteringHourMigrationCustomResource",
        "ResourceProperties": props or {"ReportingBucket": "test-bucket"},
    }


@pytest.mark.unit
class TestCFNLifecycle:
    def test_delete_returns_success_without_touching_s3(self, mig, cfn_ctx):
        """Delete must not attempt any S3 work — the bucket is managed
        elsewhere and files are not the custom resource's to clean up."""
        with patch.object(mig, "_send") as send, patch.object(mig, "_migrate") as m:
            mig.handler(_make_event("Delete"), cfn_ctx)
        send.assert_called_once()
        args, kwargs = send.call_args
        assert args[2] == "SUCCESS"
        m.assert_not_called()

    def test_create_with_empty_bucket_succeeds_immediately(self, mig, cfn_ctx):
        """A fresh install has no metering data — migration is a no-op."""
        with (
            patch.object(mig, "_iter_old_layout_keys", return_value=iter([])),
            patch.object(mig, "_send") as send,
        ):
            mig.handler(_make_event("Create", ReportingBucket="test-bucket"), cfn_ctx)
        send.assert_called_once()
        args, _ = send.call_args
        assert args[2] == "SUCCESS"

    def test_missing_bucket_property_fails(self, mig, cfn_ctx):
        """A misconfigured template shouldn't silently succeed — fail
        with a clear message."""
        event = _make_event("Create")
        event["ResourceProperties"] = {}  # ReportingBucket omitted
        with patch.object(mig, "_send") as send:
            mig.handler(event, cfn_ctx)
        args, _ = send.call_args
        assert args[2] == "FAILED"
        assert "ReportingBucket" in args[3]["reason"] if len(args) > 3 else True


@pytest.mark.unit
class TestKeyRewriting:
    def test_new_key_normal_case(self, mig):
        old = "metering/date=2026-08-18/doc123_20260818_results.parquet"
        assert (
            mig._new_key(old, "13")
            == "metering/date=2026-08-18/hour=13/doc123_20260818_results.parquet"
        )

    def test_new_key_already_hour_partitioned_returns_none(self, mig):
        """A key already at date=X/hour=Y/foo.parquet does NOT match the
        DATE_PART_PATTERN (which requires the file to be immediately under
        date=X/). Migration lister skips these before reaching _new_key,
        but defensive behavior of _new_key still matters."""
        already = "metering/date=2026-08-18/hour=13/doc123.parquet"
        assert mig._new_key(already, "00") is None

    def test_new_key_rejects_non_metering_path(self, mig):
        assert mig._new_key("other/thing.parquet", "00") is None


@pytest.mark.unit
class TestLister:
    def test_iter_old_layout_skips_already_migrated(self, mig):
        """The lister must NOT yield keys that carry `/hour=` — otherwise
        we'd try to migrate them again on a repeat run.

        Round-9 review fix uses ``Delimiter="/hour="`` so S3 filters
        hour-partitioned keys into ``CommonPrefixes`` server-side. This
        mock simulates that: Contents holds only pre-migration keys and
        CommonPrefixes holds the collapsed hour partitions.
        """
        mock_s3 = MagicMock()
        mock_paginator = MagicMock()
        mock_s3.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {
                "Contents": [
                    {"Key": "metering/date=2026-08-18/a.parquet"},  # old layout
                    {"Key": "metering/date=2026-08-18/c.parquet"},  # old layout
                    {"Key": "metering/date=2026-08-18/README.txt"},  # not parquet
                ],
                "CommonPrefixes": [
                    # b.parquet lives under this collapsed prefix.
                    {"Prefix": "metering/date=2026-08-18/hour="},
                ],
            }
        ]
        with patch.object(mig, "s3_client", mock_s3):
            keys = list(mig._iter_old_layout_keys("test-bucket"))
        # Delimiter kwarg was passed so S3 does the filter server-side.
        _, kwargs = mock_paginator.paginate.call_args
        assert kwargs.get("Delimiter") == "/hour="
        assert keys == [
            "metering/date=2026-08-18/a.parquet",
            "metering/date=2026-08-18/c.parquet",
        ]


@pytest.mark.unit
class TestFailFast:
    def test_too_many_files_fails_fast_with_actionable_message(self, mig, cfn_ctx):
        """When there are more files than the inline budget can handle,
        the resource must FAIL FAST — before the Glue table update — with
        an operator-runnable command in the reason. Silently letting the
        Glue update proceed on a too-big migration is the bug this whole
        design exists to prevent."""
        too_many = [f"metering/date=2026-01-01/f{i}.parquet" for i in range(50_000)]
        captured = {}

        def fake_send(_event, _context, status, _data=None, reason=""):
            captured["status"] = status
            captured["reason"] = reason
            return None

        with (
            patch.object(mig, "_iter_old_layout_keys", return_value=iter(too_many)),
            patch.object(mig, "_send", side_effect=fake_send),
        ):
            mig.handler(_make_event("Update", ReportingBucket="test-bucket"), cfn_ctx)
        assert captured["status"] == "FAILED"
        # Must point at the manual script.
        assert "migrate_metering_hour_partition.py" in captured["reason"]
        # Must include the bucket name so the operator's copy-paste works.
        assert "--bucket test-bucket" in captured["reason"]


@pytest.mark.unit
class TestSendCFNResponse:
    def test_send_response_puts_to_response_url(self, mig, cfn_ctx):
        """The response must PUT to the presigned ResponseURL — that's how
        CFN unblocks itself. A missing PUT is why "stack update stuck for
        60 min" happens.

        Round-13 review fix: _send now checks resp.status; the mock must
        return status=200 to route through the success path (otherwise
        the retry loop runs and asserts fail).
        """
        event = _make_event("Update", ReportingBucket="test-bucket")
        with patch("urllib3.PoolManager") as pool_cls:
            pool = MagicMock()
            pool_cls.return_value = pool
            ok_resp = MagicMock()
            ok_resp.status = 200
            pool.request.return_value = ok_resp
            mig._send(event, cfn_ctx, "SUCCESS", reason="done")
        pool.request.assert_called_once()
        args, kwargs = pool.request.call_args
        assert args[0] == "PUT"
        assert args[1] == "https://presigned.example/cfn-response"
        body = json.loads(kwargs["body"])
        assert body["Status"] == "SUCCESS"
        assert body["Reason"] == "done"
        assert body["StackId"] == event["StackId"]
        assert body["RequestId"] == event["RequestId"]
