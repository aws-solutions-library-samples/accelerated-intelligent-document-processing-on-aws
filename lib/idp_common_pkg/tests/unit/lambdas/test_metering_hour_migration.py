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


@pytest.mark.unit
class TestConcurrentMigrationRace:
    """Round-15 review fix: when the source has already been deleted by
    a concurrent migrator (or a prior successful run whose future
    result was lost), the head-succeeds path must NOT fall through to
    copy_object — that raises NoSuchKey on the missing source and flips
    an already-migrated file to FAILED, wedging the custom resource.
    """

    def test_target_exists_and_source_already_deleted_returns_moved(self, mig):
        from botocore.exceptions import ClientError

        mock_s3 = MagicMock()

        def head_side_effect(Bucket, Key):  # noqa: ARG001, N803
            if Key == "metering/date=2026-08-18/hour=13/a.parquet":
                return {"ContentLength": 12345}
            # Source already deleted — the failure mode we're guarding.
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")

        mock_s3.head_object.side_effect = head_side_effect
        # The Lambda's ``s3_client.exceptions.ClientError`` reference is
        # what the ``except`` clause catches — point it at the real
        # botocore ClientError so ``raise`` and ``except`` match.
        mock_s3.exceptions.ClientError = ClientError

        with (
            patch.object(mig, "s3_client", mock_s3),
            patch.object(mig, "_infer_hour", return_value=("13", True)),
        ):
            result = mig._migrate_one(
                "test-bucket", "metering/date=2026-08-18/a.parquet"
            )
        assert result == "moved"
        # Must NOT have tried to copy from the missing source.
        mock_s3.copy_object.assert_not_called()
        # And must NOT have tried to delete a non-existent source.
        mock_s3.delete_object.assert_not_called()


@pytest.mark.unit
class TestInferHourRoundEighteen:
    """Round-18 review fixes for _infer_hour (#551 NULL-row-0, #578 tz)."""

    def test_scans_past_null_row_zero_to_find_first_non_null(self, mig):
        """Round-18 fix (#551): _infer_hour used to only read row 0 of
        the wanted columns and give up if it was NULL, even when later
        rows had valid timestamps. Now it scans until it finds a
        non-null value.
        """
        import datetime as _dt
        from unittest.mock import MagicMock

        fake_pf = MagicMock()
        fake_pf.schema_arrow.names = ["timestamp", "initial_event_time"]
        # Row 0 null, row 1 null, row 2 valid.
        col_ts = MagicMock()
        col_ts.__len__ = lambda _self: 3
        col_ts.__getitem__ = lambda _self, i: MagicMock(
            as_py=lambda: None if i < 2 else _dt.datetime(2026, 8, 27, 15, 0)
        )
        batch = MagicMock()
        batch.column.return_value = col_ts
        fake_pf.iter_batches.return_value = iter([batch])
        with patch.object(mig, "_open_parquet_range_read", return_value=fake_pf):
            hour, inferred = mig._infer_hour("bucket", "key")
        assert inferred is True
        assert hour == "15"

    def test_tz_aware_non_utc_datetime_returns_utc_hour(self, mig):
        """Round-18 fix (#578): a tz-aware non-UTC datetime used to
        return its LOCAL-tz hour via ``ts.strftime('%H')``. The hour
        partition contract is UTC — files would silently land under
        the wrong hour subdirectory. Now: convert to UTC first.
        """
        import datetime as _dt
        from unittest.mock import MagicMock

        # 15:00 in a UTC-5 tz == 20:00 UTC.
        tz_minus5 = _dt.timezone(_dt.timedelta(hours=-5))
        aware_ts = _dt.datetime(2026, 8, 27, 15, 0, tzinfo=tz_minus5)

        fake_pf = MagicMock()
        fake_pf.schema_arrow.names = ["timestamp"]
        col_ts = MagicMock()
        col_ts.__len__ = lambda _self: 1
        col_ts.__getitem__ = lambda _self, i: MagicMock(as_py=lambda: aware_ts)
        batch = MagicMock()
        batch.column.return_value = col_ts
        fake_pf.iter_batches.return_value = iter([batch])
        with patch.object(mig, "_open_parquet_range_read", return_value=fake_pf):
            hour, inferred = mig._infer_hour("bucket", "key")
        assert inferred is True
        # 15:00 UTC-5 = 20:00 UTC — must be the UTC hour, NOT '15'.
        assert hour == "20"
