"""Unit tests for cleanup_stale_idp_stacks — the IAM-role-leak startup reaper.

The reaper is the durability guarantee that leaked `-iam` stacks (and their
per-run roles) can't accumulate and exhaust the account RolesPerAccount quota
when a run's own cleanup is interrupted. These tests mock boto3 so they need no
AWS: they verify the age gate (never delete an in-flight concurrent run), the
nested-stack skip (only top-level stacks are deleted; parents cascade), the
apigw-vpc skip (owned by the other reaper), and the main-before-iam delete
ordering.
"""

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.unit


class _FakePaginator:
    def __init__(self, summaries):
        self._summaries = summaries

    def paginate(self, **kwargs):
        return [{"StackSummaries": self._summaries}]


class _FakeCfn:
    def __init__(self, summaries):
        self._summaries = summaries
        self.deleted = []

    def get_paginator(self, name):
        return _FakePaginator(self._summaries)

    def delete_stack(self, StackName):
        self.deleted.append(StackName)


def _summary(name, age_seconds, root=None, parent=None):
    now = datetime.now(tz=timezone.utc)
    s = {
        "StackName": name,
        "CreationTime": now - timedelta(seconds=age_seconds),
    }
    if root:
        s["RootId"] = root
    if parent:
        s["ParentId"] = parent
    return s


def _install(cbd, monkeypatch, summaries):
    fake = _FakeCfn(summaries)
    monkeypatch.setattr(cbd.boto3, "client", lambda name, *a, **k: fake)
    return fake


def test_reaper_deletes_old_stacks(cbd, monkeypatch):
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    fake = _install(
        cbd,
        monkeypatch,
        [
            _summary("idp-0709-211927", old),
            _summary("idp-0709-211927-iam", old),
        ],
    )
    cbd.cleanup_stale_idp_stacks()
    assert set(fake.deleted) == {"idp-0709-211927", "idp-0709-211927-iam"}


def test_reaper_skips_young_stacks(cbd, monkeypatch):
    # A stack younger than the age gate could be a concurrent pipeline's
    # in-flight run — must NOT be deleted.
    young = cbd.IDP_STACK_STALE_AGE_SECONDS - 600
    fake = _install(
        cbd,
        monkeypatch,
        [
            _summary("idp-0716-150000", young),
            _summary("idp-0716-150000-iam", young),
        ],
    )
    cbd.cleanup_stale_idp_stacks()
    assert fake.deleted == []


def test_reaper_skips_nested_stacks(cbd, monkeypatch):
    # Nested stacks (RootId/ParentId set) are deleted by their parent cascade,
    # never directly.
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    fake = _install(
        cbd,
        monkeypatch,
        [
            _summary("idp-0709-211927", old),
            _summary(
                "idp-0709-211927-PATTERNSTACK-ABC",
                old,
                root="arn:...:stack/idp-0709-211927/x",
                parent="arn:...:stack/idp-0709-211927/x",
            ),
        ],
    )
    cbd.cleanup_stale_idp_stacks()
    assert fake.deleted == ["idp-0709-211927"]


def test_reaper_skips_apigw_vpc(cbd, monkeypatch):
    # *-apigw-vpc is owned by cleanup_stale_apigw_test_vpcs (ENI-aware delete);
    # this reaper must leave it alone.
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    fake = _install(
        cbd,
        monkeypatch,
        [
            _summary("idp-0715-200655-apigw-vpc", old),
            _summary("idp-0709-211927", old),
        ],
    )
    cbd.cleanup_stale_idp_stacks()
    assert fake.deleted == ["idp-0709-211927"]


def test_reaper_deletes_main_before_iam(cbd, monkeypatch):
    # The main stack references its -iam CFServiceRole; deleting -iam first can
    # strand the main stack. Order: non-iam stacks first, then -iam.
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    fake = _install(
        cbd,
        monkeypatch,
        [
            _summary("idp-0709-211927-iam", old),
            _summary("idp-0709-211927", old),
            _summary("idp-0709-211927-headless", old),
            _summary("idp-0709-211927-headless-iam", old),
        ],
    )
    cbd.cleanup_stale_idp_stacks()
    # Every -iam delete must come after all non-iam deletes.
    last_non_iam = max(
        i for i, n in enumerate(fake.deleted) if not n.endswith("-iam")
    )
    first_iam = min(i for i, n in enumerate(fake.deleted) if n.endswith("-iam"))
    assert last_non_iam < first_iam
    assert len(fake.deleted) == 4


def test_reaper_ignores_non_idp_stacks(cbd, monkeypatch):
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    fake = _install(
        cbd,
        monkeypatch,
        [
            _summary("genaiic-sdlc-codepipeline", old),  # the pipeline stack itself
            _summary("some-other-stack", old),
            _summary("idp-0709-211927", old),
        ],
    )
    cbd.cleanup_stale_idp_stacks()
    assert fake.deleted == ["idp-0709-211927"]


def test_reaper_never_raises_on_api_error(cbd, monkeypatch):
    class _Boom:
        def get_paginator(self, name):
            raise RuntimeError("throttled")

    monkeypatch.setattr(cbd.boto3, "client", lambda name, *a, **k: _Boom())
    # Best-effort: must swallow the error, not propagate it.
    cbd.cleanup_stale_idp_stacks()


# ---------------------------------------------------------------------------
# Retention markers
#
# A failing run may deliberately keep its stack so its Step Functions
# histories, tracking table and log groups can still be read. The marker it
# writes is the ONLY thing holding this reaper off, which makes the reaper the
# place where retention either stays bounded or becomes a role-quota leak.
# ---------------------------------------------------------------------------

import json  # noqa: E402

RETENTION_BUCKET = "genaiic-sdlc-sourcecode-1234-us-east-1"


class _FakeCfnAndS3(_FakeCfn):
    """One fake standing in for both clients, since boto3.client is patched once.

    `list_stacks` and `list_objects_v2` are told apart by paginator name; the
    reaper asks for both.
    """

    def __init__(self, summaries, markers):
        super().__init__(summaries)
        # markers: {stack_name: keep_until_iso_or_None}
        self._markers = markers
        self.deleted_objects = []

    def get_paginator(self, name):
        if name == "list_objects_v2":
            keys = [
                {"Key": f"ci-retained/{stack}.json"} for stack in sorted(self._markers)
            ]
            return _ObjectPaginator(keys)
        return _FakePaginator(self._summaries)

    def get_object(self, Bucket, Key):  # noqa: N803
        stack = Key.rsplit("/", 1)[-1].removesuffix(".json")
        payload = json.dumps({"keep_until": self._markers[stack] or ""}).encode()

        class _B:
            def read(self):
                return payload

        return {"Body": _B()}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.deleted_objects.append(Key)


class _ObjectPaginator:
    def __init__(self, keys):
        self._keys = keys

    def paginate(self, **kwargs):
        return [{"Contents": self._keys}]


def _install_with_markers(cbd, monkeypatch, summaries, markers):
    monkeypatch.setenv("SOURCE_BUCKET", RETENTION_BUCKET)
    fake = _FakeCfnAndS3(summaries, markers)
    monkeypatch.setattr(cbd.boto3, "client", lambda name, *a, **k: fake)
    return fake


def _in(hours):
    return (datetime.now(tz=timezone.utc) + timedelta(hours=hours)).isoformat()


def _ago(hours):
    return (datetime.now(tz=timezone.utc) - timedelta(hours=hours)).isoformat()


def test_a_retained_stack_is_not_reaped_while_its_marker_is_in_the_future(
    cbd, monkeypatch
):
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    fake = _install_with_markers(
        cbd,
        monkeypatch,
        [_summary("idp-0709-211927", old)],
        {"idp-0709-211927": _in(6)},
    )
    cbd.cleanup_stale_idp_stacks()
    assert fake.deleted == []


def test_retention_also_protects_the_iam_stack_of_the_same_run(cbd, monkeypatch):
    """Protection is matched on the run prefix, not the exact stack name.

    The `-iam` stack holds the CFServiceRole and permissions boundary the main
    stack was deployed with. Reaping it while keeping the main stack would leave
    a stack that can no longer be deleted cleanly — so a marker for
    `idp-MMDD-HHMMSS` has to cover `idp-MMDD-HHMMSS-iam` too.
    """
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    fake = _install_with_markers(
        cbd,
        monkeypatch,
        [
            _summary("idp-0709-211927", old),
            _summary("idp-0709-211927-iam", old),
        ],
        {"idp-0709-211927": _in(6)},
    )
    cbd.cleanup_stale_idp_stacks()
    assert fake.deleted == []


def test_an_expired_marker_lets_the_stack_be_reaped_and_drops_the_marker(
    cbd, monkeypatch
):
    """Expiry is what makes retention bounded rather than a leak."""
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    fake = _install_with_markers(
        cbd,
        monkeypatch,
        [
            _summary("idp-0709-211927", old),
            _summary("idp-0709-211927-iam", old),
        ],
        {"idp-0709-211927": _ago(1)},
    )
    cbd.cleanup_stale_idp_stacks()
    assert set(fake.deleted) == {"idp-0709-211927", "idp-0709-211927-iam"}
    assert fake.deleted_objects == ["ci-retained/idp-0709-211927.json"]


def test_an_expired_marker_overrides_the_age_gate(cbd, monkeypatch):
    """A retained stack younger than the gate is still reaped once expired.

    Without this a short TTL would be ignored for the first three hours, and the
    retention slot would stay occupied past the deadline the run asked for.
    """
    young = cbd.IDP_STACK_STALE_AGE_SECONDS - 600
    fake = _install_with_markers(
        cbd,
        monkeypatch,
        [_summary("idp-0716-150000", young)],
        {"idp-0716-150000": _ago(1)},
    )
    cbd.cleanup_stale_idp_stacks()
    assert fake.deleted == ["idp-0716-150000"]


def test_a_marker_for_an_already_deleted_stack_is_dropped(cbd, monkeypatch):
    """Otherwise it would hold a retention slot forever.

    Enough of these and the ceiling is permanently reached, which turns
    retention off for every later run while still looking enabled.
    """
    fake = _install_with_markers(
        cbd, monkeypatch, [], {"idp-0101-000001": _ago(99)}
    )
    cbd.cleanup_stale_idp_stacks()
    assert fake.deleted == []
    assert fake.deleted_objects == ["ci-retained/idp-0101-000001.json"]


def test_an_unparseable_marker_does_not_protect_its_stack(cbd, monkeypatch):
    """"Cannot tell" must mean "reap it", or a bad write leaks a stack."""
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    fake = _install_with_markers(
        cbd,
        monkeypatch,
        [_summary("idp-0709-211927", old)],
        {"idp-0709-211927": None},
    )
    cbd.cleanup_stale_idp_stacks()
    assert fake.deleted == ["idp-0709-211927"]
