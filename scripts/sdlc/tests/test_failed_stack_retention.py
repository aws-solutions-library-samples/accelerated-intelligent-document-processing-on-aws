"""Unit tests for failed-stack retention and the diagnostics bundle.

Retention keeps a failed CI stack alive so its Step Functions histories,
tracking table and Lambda log groups can still be read — all of which teardown
destroys within minutes, and none of which a snapshot can be guaranteed to have
anticipated.

**The thing these tests are really protecting is the account's IAM role
headroom.** One IDP stack carries ~122 roles against a quota of 5000, and role
exhaustion fails *every* deploy in the account — the exact condition
`cleanup_stale_idp_stacks` was written for after ~600 leaked roles did it once.
So retention is only safe while three claims hold, and each is pinned here:

* a retained stack is eventually reaped, whatever happens to the run that
  retained it (the marker carries a deadline; the startup reaper enforces it),
* no more than `MAX_RETAINED_STACKS` are ever held at once, and
* a marker that cannot be read, or whose stack is gone, does not hold a slot.

The third is the subtle one: a marker is the only thing standing between a
retained stack and the reaper, so every way a marker can be malformed has to
fail *towards* reaping.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.unit

BUCKET = "genaiic-sdlc-sourcecode-1234-us-east-1"


class _FakeS3:
    """An in-memory S3 with just the operations the retention code uses."""

    def __init__(self, objects=None):
        # {key: (body_bytes, last_modified)}
        self.objects = dict(objects or {})
        self.deleted = []
        self.put = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        outer = self

        class _P:
            def paginate(self, Bucket, Prefix="", **kwargs):  # noqa: N803
                contents = [
                    {"Key": key, "LastModified": modified}
                    for key, (_, modified) in sorted(outer.objects.items())
                    if key.startswith(Prefix)
                ]
                return [{"Contents": contents}]

        return _P()

    def get_object(self, Bucket, Key):  # noqa: N803
        body, _ = self.objects[Key]
        if isinstance(body, Exception):
            raise body

        class _B:
            def read(self):
                return body

        return {"Body": _B()}

    def put_object(self, Bucket, Key, Body, **kwargs):  # noqa: N803
        self.objects[Key] = (Body, datetime.now(tz=timezone.utc))
        self.put.append(Key)

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.deleted.append(Key)
        self.objects.pop(Key, None)

    def upload_file(self, filename, bucket, key):
        self.objects[key] = (b"", datetime.now(tz=timezone.utc))
        self.put.append(key)


def _marker(stack_name, keep_until):
    return json.dumps({"stack_name": stack_name, "keep_until": keep_until}).encode()


def _future(hours=6):
    return (datetime.now(tz=timezone.utc) + timedelta(hours=hours)).isoformat()


def _past(hours=6):
    return (datetime.now(tz=timezone.utc) - timedelta(hours=hours)).isoformat()


@pytest.fixture
def s3(cbd, monkeypatch):
    """A fake S3 installed over boto3, with SOURCE_BUCKET set."""
    monkeypatch.setenv("SOURCE_BUCKET", BUCKET)
    fake = _FakeS3()
    monkeypatch.setattr(cbd.boto3, "client", lambda name, *a, **k: fake)
    return fake


class TestRetainFailedStack:
    def test_a_marker_is_written_with_a_deadline_in_the_future(self, cbd, s3):
        assert cbd.retain_failed_stack("idp-1009-072103") is True

        key = f"{cbd.RETENTION_S3_PREFIX}/idp-1009-072103.json"
        assert key in s3.objects
        body = json.loads(s3.objects[key][0])
        assert body["stack_name"] == "idp-1009-072103"
        assert datetime.fromisoformat(body["keep_until"]) > datetime.now(
            tz=timezone.utc
        )

    def test_the_ttl_is_clamped_to_the_maximum(self, cbd, s3, monkeypatch):
        """A TTL longer than the cap must not be honoured.

        The cap is the only thing bounding how long the role quota stays
        consumed, so an env var is not allowed to override it — a typo of one
        extra zero would otherwise hold six stacks for a month.
        """
        monkeypatch.setattr(cbd, "KEEP_FAILED_STACK_HOURS", 24 * 365)

        cbd.retain_failed_stack("idp-1009-072103")

        body = json.loads(
            s3.objects[f"{cbd.RETENTION_S3_PREFIX}/idp-1009-072103.json"][0]
        )
        held_for = datetime.fromisoformat(body["keep_until"]) - datetime.now(
            tz=timezone.utc
        )
        assert held_for <= timedelta(hours=cbd.KEEP_FAILED_STACK_MAX_HOURS)

    def test_the_ceiling_declines_retention_rather_than_exceeding_it(
        self, cbd, s3, monkeypatch
    ):
        """At the ceiling the answer is False, so the caller tears down.

        Returning True here would be the quota-exhaustion bug: every failing run
        in a bad week would keep its stack and the account would stop being able
        to deploy at all.
        """
        monkeypatch.setattr(cbd, "MAX_RETAINED_STACKS", 2)
        for name in ("idp-0101-000001", "idp-0101-000002"):
            s3.objects[f"{cbd.RETENTION_S3_PREFIX}/{name}.json"] = (
                _marker(name, _future()),
                datetime.now(tz=timezone.utc),
            )

        assert cbd.retain_failed_stack("idp-1009-072103") is False
        assert f"{cbd.RETENTION_S3_PREFIX}/idp-1009-072103.json" not in s3.objects

    def test_expired_markers_do_not_occupy_a_slot(self, cbd, s3, monkeypatch):
        """Only markers still in the future count towards the ceiling.

        Otherwise the ceiling would be reached permanently after
        MAX_RETAINED_STACKS failures ever, and retention would switch itself off
        while still appearing to be enabled.
        """
        monkeypatch.setattr(cbd, "MAX_RETAINED_STACKS", 2)
        for name in ("idp-0101-000001", "idp-0101-000002"):
            s3.objects[f"{cbd.RETENTION_S3_PREFIX}/{name}.json"] = (
                _marker(name, _past()),
                datetime.now(tz=timezone.utc),
            )

        assert cbd.retain_failed_stack("idp-1009-072103") is True

    def test_re_retaining_the_same_stack_is_not_blocked_by_its_own_marker(
        self, cbd, s3, monkeypatch
    ):
        """A stack already holding a slot does not need a second one."""
        monkeypatch.setattr(cbd, "MAX_RETAINED_STACKS", 1)
        s3.objects[f"{cbd.RETENTION_S3_PREFIX}/idp-1009-072103.json"] = (
            _marker("idp-1009-072103", _future()),
            datetime.now(tz=timezone.utc),
        )

        assert cbd.retain_failed_stack("idp-1009-072103") is True

    def test_retention_can_be_switched_off(self, cbd, s3, monkeypatch):
        monkeypatch.setattr(cbd, "KEEP_FAILED_STACK", False)
        assert cbd.retain_failed_stack("idp-1009-072103") is False
        assert s3.put == []

    def test_a_missing_source_bucket_declines_rather_than_silently_keeping(
        self, cbd, monkeypatch
    ):
        """With nowhere to record the marker, the stack must be torn down.

        Retaining without a marker would produce a stack no reaper knows to
        collect — a permanent leak, which is strictly worse than losing the
        evidence.
        """
        monkeypatch.delenv("SOURCE_BUCKET", raising=False)
        assert cbd.retain_failed_stack("idp-1009-072103") is False

    def test_an_s3_failure_declines_rather_than_silently_keeping(
        self, cbd, monkeypatch
    ):
        monkeypatch.setenv("SOURCE_BUCKET", BUCKET)

        class _Broken:
            def get_paginator(self, name):
                raise RuntimeError("s3 is down")

        monkeypatch.setattr(cbd.boto3, "client", lambda name, *a, **k: _Broken())
        assert cbd.retain_failed_stack("idp-1009-072103") is False


class TestKeepUntilParsing:
    """Every malformed `keep_until` must read as expired, not as protected."""

    def test_a_future_instant_is_protected(self, cbd):
        now = datetime.now(tz=timezone.utc)
        assert cbd._keep_until_in_future(_future(), now) is True

    @pytest.mark.parametrize(
        "value",
        ["", None, "not-a-date", "whenever", "2026-13-45T99:99:99"],
        ids=["empty", "none", "garbage", "word", "out-of-range"],
    )
    def test_an_unusable_value_reads_as_expired(self, cbd, value):
        """A marker nobody can parse must not protect its stack forever.

        This is the direction that matters: the marker is the only thing holding
        the reaper off, so "cannot tell" has to mean "reap it".
        """
        now = datetime.now(tz=timezone.utc)
        assert cbd._keep_until_in_future(value, now) is False

    def test_a_past_instant_reads_as_expired(self, cbd):
        now = datetime.now(tz=timezone.utc)
        assert cbd._keep_until_in_future(_past(), now) is False

    def test_a_naive_instant_is_read_as_utc(self, cbd):
        """The marker round-trips through JSON, so the offset can be absent."""
        now = datetime.now(tz=timezone.utc)
        naive = (now + timedelta(hours=3)).replace(tzinfo=None).isoformat()
        assert cbd._keep_until_in_future(naive, now) is True

    def test_an_unreadable_marker_body_reads_as_expired(self, cbd, s3):
        """A marker whose GetObject raises must not protect its stack."""
        s3.objects[f"{cbd.RETENTION_S3_PREFIX}/idp-0101-000001.json"] = (
            RuntimeError("denied"),
            datetime.now(tz=timezone.utc),
        )

        protected, expired = cbd._expired_retention_markers(
            BUCKET, datetime.now(tz=timezone.utc)
        )

        assert protected == {}
        assert expired == ["idp-0101-000001"]


class TestDiagnosticsBundle:
    def test_the_bundle_is_keyed_by_build_and_stack(self, cbd, s3, monkeypatch):
        """One build deploys several stacks, each able to fail on its own."""
        monkeypatch.setenv("CODEBUILD_BUILD_ID", "app-sdlc:abc-123")

        uri = cbd.persist_diagnostics_bundle("idp-1009-072103", {"error": "boom"})

        assert uri == (
            f"s3://{BUCKET}/{cbd.DIAGNOSTICS_S3_PREFIX}/"
            "app-sdlc_abc-123/idp-1009-072103/evidence.json"
        )

    def test_the_bundle_survives_unserialisable_values(self, cbd, s3):
        """Evidence is assembled from boto3 responses, which hold datetimes.

        A `TypeError` from `json.dumps` here would lose the entire bundle for the
        sake of one field, at the one moment it cannot be regenerated.
        """
        bundle = {"collected_at": datetime.now(tz=timezone.utc)}

        assert cbd.persist_diagnostics_bundle("idp-1009-072103", bundle) is not None

    def test_no_source_bucket_returns_none_without_raising(self, cbd, monkeypatch):
        monkeypatch.delenv("SOURCE_BUCKET", raising=False)
        assert cbd.persist_diagnostics_bundle("idp-1009-072103", {}) is None


class TestDiagnosticsExpiry:
    def test_only_objects_past_the_retention_window_are_deleted(self, cbd, s3):
        now = datetime.now(tz=timezone.utc)
        old = now - timedelta(seconds=cbd.DIAGNOSTICS_RETENTION_SECONDS + 3600)
        s3.objects = {
            f"{cbd.DIAGNOSTICS_S3_PREFIX}/old/evidence.json": (b"{}", old),
            f"{cbd.DIAGNOSTICS_S3_PREFIX}/new/evidence.json": (b"{}", now),
        }

        cbd.cleanup_stale_ci_diagnostics()

        assert s3.deleted == [f"{cbd.DIAGNOSTICS_S3_PREFIX}/old/evidence.json"]

    def test_nothing_outside_the_diagnostics_prefix_is_touched(self, cbd, s3):
        """The bucket also holds published templates and the source archive."""
        old = datetime.now(tz=timezone.utc) - timedelta(
            seconds=cbd.DIAGNOSTICS_RETENTION_SECONDS + 3600
        )
        s3.objects = {
            "deploy/code.zip": (b"", old),
            "idp-main.yaml": (b"", old),
            f"{cbd.RETENTION_S3_PREFIX}/idp-0101-000001.json": (b"{}", old),
        }

        cbd.cleanup_stale_ci_diagnostics()

        assert s3.deleted == []
