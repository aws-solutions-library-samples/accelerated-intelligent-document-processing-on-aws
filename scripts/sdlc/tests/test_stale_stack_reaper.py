"""Unit tests for cleanup_stale_idp_stacks — the IAM-role-leak startup reaper.

The reaper is the durability guarantee that leaked `-iam` stacks (and their
per-run roles) can't accumulate and exhaust the account RolesPerAccount quota
when a run's own cleanup is interrupted. These tests mock boto3 so they need no
AWS: they verify the age gate (never delete an in-flight concurrent run), the
nested-stack skip (only top-level stacks are deleted; parents cascade), the
apigw-vpc skip (owned by the other reaper), and that a run's -iam stack is
never deleted in the same pass as its main stack: it waits until the main stack
is gone. When that main stack is stuck (DELETE_FAILED, or wedged in a state the
listing omits) or its existence cannot be established, the -iam stack is kept and
the pair is warned about rather than turned into a finding that reddens the run.
"""

from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

pytestmark = pytest.mark.unit


class _FakePaginator:
    def __init__(self, summaries):
        self._summaries = summaries

    def paginate(self, **kwargs):
        return [{"StackSummaries": self._summaries}]


class _FakeCfn:
    """list_stacks, delete_stack and describe_stacks as CloudFormation answers
    them: a stack whose delete was just issued is still DELETE_IN_PROGRESS (a
    delete_stack call only starts the delete), a listed stack exists, and any
    other name does not. ``in_progress`` adds stacks that exist but are left out
    of the reaper's listing, as DELETE_IN_PROGRESS ones are; ``statuses`` answers
    a name with a given status ahead of all of that."""

    def __init__(self, summaries, in_progress=(), statuses=None):
        self._summaries = summaries
        self._in_progress = set(in_progress)
        self._statuses = dict(statuses or {})
        self.deleted = []
        self.described = []

    def get_paginator(self, name):
        return _FakePaginator(self._summaries)

    def delete_stack(self, StackName):
        self.deleted.append(StackName)

    def describe_stacks(self, StackName):
        self.described.append(StackName)
        listed = {x["StackName"] for x in self._summaries}
        if StackName in self._statuses:
            return {
                "Stacks": [
                    {"StackName": StackName, "StackStatus": self._statuses[StackName]}
                ]
            }
        if StackName in self.deleted or StackName in self._in_progress:
            return {
                "Stacks": [
                    {"StackName": StackName, "StackStatus": "DELETE_IN_PROGRESS"}
                ]
            }
        if StackName in listed:
            return {
                "Stacks": [{"StackName": StackName, "StackStatus": "CREATE_COMPLETE"}]
            }
        raise ClientError(
            {
                "Error": {
                    "Code": "ValidationError",
                    "Message": f"Stack with id {StackName} does not exist",
                }
            },
            "DescribeStacks",
        )


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


def _install(cbd, monkeypatch, summaries, in_progress=(), statuses=None):
    fake = _FakeCfn(summaries, in_progress=in_progress, statuses=statuses)
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
    # The -iam stack waits: its main stack is still deleting through its role.
    assert fake.deleted == ["idp-0709-211927"]


def test_an_iam_stack_whose_main_stack_is_gone_is_reaped(cbd, monkeypatch):
    """The later night: the main stack has finished deleting, so its -iam stack
    (and the role in it) goes too."""
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    fake = _install(cbd, monkeypatch, [_summary("idp-0709-211927-iam", old)])
    cbd.cleanup_stale_idp_stacks()
    assert fake.deleted == ["idp-0709-211927-iam"]
    assert fake.described == ["idp-0709-211927"]


def test_an_iam_stack_waits_for_a_main_stack_the_listing_omits(cbd, monkeypatch):
    """The listing leaves out DELETE_IN_PROGRESS stacks, the one state in which
    removing the -iam stack strands the main delete; so existence is asked
    directly."""
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    fake = _install(
        cbd,
        monkeypatch,
        [_summary("idp-0709-211927-iam", old)],
        in_progress={"idp-0709-211927"},
    )
    cbd.cleanup_stale_idp_stacks()
    assert fake.deleted == []


def test_an_unanswered_existence_check_keeps_the_role(cbd, monkeypatch, capsys):
    """Fails closed: a describe error other than "does not exist" defers."""
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    fake = _install(cbd, monkeypatch, [_summary("idp-0709-211927-iam", old)])

    def throttled(StackName):
        raise ClientError(
            {"Error": {"Code": "Throttling", "Message": "Rate exceeded"}},
            "DescribeStacks",
        )

    fake.describe_stacks = throttled
    assert cbd.cleanup_stale_idp_stacks() == []
    assert fake.deleted == []
    assert "could not establish whether idp-0709-211927" in capsys.readouterr().out


def test_an_error_merely_mentioning_does_not_exist_keeps_the_role(cbd, monkeypatch):
    """Only a ValidationError saying so means gone; an AccessDenied whose message
    happens to contain the phrase is an unanswered check, and defers."""
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    fake = _install(cbd, monkeypatch, [_summary("idp-0709-211927-iam", old)])

    def denied(StackName):
        raise ClientError(
            {
                "Error": {
                    "Code": "AccessDenied",
                    "Message": "role does not exist or is not authorized",
                }
            },
            "DescribeStacks",
        )

    fake.describe_stacks = denied
    cbd.cleanup_stale_idp_stacks()
    assert fake.deleted == []


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


def test_reaper_never_deletes_an_iam_stack_in_the_same_pass_as_its_main(
    cbd, monkeypatch
):
    # The main stack deletes through its -iam CFServiceRole, and delete_stack only
    # starts a delete: removing the -iam stack in the same pass stranded the main
    # stack DELETE_FAILED with no role to retry through.
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
    assert sorted(fake.deleted) == ["idp-0709-211927", "idp-0709-211927-headless"]


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
        marker = self._markers[stack]
        # An Exception value means the API call itself does not answer, which is
        # a different fact from a body nobody can parse and must not be driven
        # through the same door. `bytes` is a body returned verbatim, which is
        # how a malformed one is expressed.
        if isinstance(marker, Exception):
            raise marker
        if isinstance(marker, bytes):
            payload = marker
        else:
            payload = json.dumps({"keep_until": marker or ""}).encode()

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
    assert fake.deleted == ["idp-0709-211927"]  # its -iam stack follows once it is gone
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
    fake = _install_with_markers(cbd, monkeypatch, [], {"idp-0101-000001": _ago(99)})
    cbd.cleanup_stale_idp_stacks()
    assert fake.deleted == []
    assert fake.deleted_objects == ["ci-retained/idp-0101-000001.json"]


def test_an_unparseable_marker_does_not_protect_its_stack(cbd, monkeypatch):
    """ "Cannot tell" must mean "reap it", or a bad write leaks a stack."""
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    fake = _install_with_markers(
        cbd,
        monkeypatch,
        [_summary("idp-0709-211927", old)],
        {"idp-0709-211927": None},
    )
    cbd.cleanup_stale_idp_stacks()
    assert fake.deleted == ["idp-0709-211927"]


# ---------------------------------------------------------------------------
# The two ways a marker read can fail are two different facts (#1445)
#
# A marker whose BODY cannot be understood is the case the expiry override is
# right about: nobody can read it, and left in place it would pin a retention
# slot forever. A marker whose READ did not answer is not that — it establishes
# nothing about what the marker says, so expiring on it reaps a stack a failed
# run deliberately asked to keep, which is the one thing retention exists for.
# Both directions are driven, because a change that keeps everything makes the
# retention half of this reaper inert.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"{not json at all", id="not-json"),
        pytest.param(b'{"stack_name": "idp-0709-211927"}', id="no-keep-until"),
        pytest.param(b'{"keep_until": "whenever"}', id="unparseable-instant"),
    ],
)
def test_a_marker_body_that_cannot_be_parsed_still_expires(cbd, monkeypatch, body):
    """Unchanged by the split: the answer arrived and is unusable."""
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    fake = _install_with_markers(
        cbd,
        monkeypatch,
        [_summary("idp-0709-211927", old)],
        {"idp-0709-211927": body},
    )

    findings = cbd.cleanup_stale_idp_stacks()

    assert fake.deleted == ["idp-0709-211927"]
    assert fake.deleted_objects == ["ci-retained/idp-0709-211927.json"]
    assert findings == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        pytest.param(
            ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "not authorized"}},
                "GetObject",
            ),
            "AccessDenied",
            id="denied",
        ),
        pytest.param(
            ClientError(
                {"Error": {"Code": "ExpiredToken", "Message": "token expired"}},
                "GetObject",
            ),
            "ExpiredToken",
            id="expired-credentials",
        ),
        pytest.param(
            ClientError(
                {"Error": {"Code": "SlowDown", "Message": "please reduce"}},
                "GetObject",
            ),
            "SlowDown",
            id="retry-budget-exhausted",
        ),
        pytest.param(
            EndpointConnectionError(endpoint_url="https://s3"),
            "Could not connect",
            id="no-route",
        ),
    ],
)
def test_a_marker_whose_read_did_not_answer_keeps_the_stack(
    cbd, monkeypatch, capsys, error, expected
):
    """The stack is KEPT, counted, named — and the marker is left alone.

    Kept even though it is older than the age gate, because the marker overrides
    the gate in both directions and this one may say "keep until tomorrow". The
    next run makes the same GetObject, so the retention slot still frees up.
    """
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    fake = _install_with_markers(
        cbd,
        monkeypatch,
        [_summary("idp-0709-211927", old), _summary("idp-0709-211927-iam", old)],
        {"idp-0709-211927": error},
    )

    findings = cbd.cleanup_stale_idp_stacks()

    assert fake.deleted == []
    assert fake.deleted_objects == []
    # Reported in its own counter rather than as an incomplete sweep: every
    # stack the listing returned was considered, and a red night's run over one
    # skipped item would cost the row that reports a genuinely inert sweep.
    assert findings == []
    out = capsys.readouterr().out
    assert "idp-0709-211927" in out
    assert "retention marker could not be read" in out
    # Carrying WHY, because a stack kept for a reason the log does not state is
    # one nobody can act on — and "denied" and "the retry budget ran out" are
    # different things to do next.
    assert expected in out, out
    assert cbd.SWEEP_NOT_COMPLETED not in out


def test_one_unreadable_marker_does_not_protect_every_other_run(cbd, monkeypatch):
    """Per run, not per sweep: the rest of the account is still reaped.

    Keeping everything because one marker could not be read would make the
    reaper inert, which is the defect in the other direction.
    """
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    fake = _install_with_markers(
        cbd,
        monkeypatch,
        [_summary("idp-0709-211927", old), _summary("idp-0808-120000", old)],
        {
            "idp-0709-211927": ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetObject"
            ),
            "idp-0808-120000": _ago(1),
        },
    )

    assert cbd.cleanup_stale_idp_stacks() == []
    assert fake.deleted == ["idp-0808-120000"]
    assert fake.deleted_objects == ["ci-retained/idp-0808-120000.json"]


def test_a_main_stack_that_already_failed_to_delete_is_warned_not_failed(
    cbd, monkeypatch, capsys
):
    """Waiting converges for a delete in progress, not for one that already failed:
    the role is kept for the retry, and the pair is warned about so it cannot pin
    its roles quietly -- without reddening every build until someone fixes it."""
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    failed = _summary("idp-0709-211927", old)
    failed["StackStatus"] = "DELETE_FAILED"
    fake = _install(cbd, monkeypatch, [failed, _summary("idp-0709-211927-iam", old)])
    abandoned = []
    assert cbd.cleanup_stale_idp_stacks(abandoned=abandoned) == []
    assert fake.deleted == ["idp-0709-211927"]  # retried; the role is kept for it
    out = capsys.readouterr().out
    assert "idp-0709-211927-iam kept" in out
    assert "DELETE_FAILED" in out
    assert "retried this run" in out
    assert "waiting will not resolve" not in out
    # A retry that keeps failing recurs every night; the summary names it.
    assert len(abandoned) == 1 and "retried this run" in abandoned[0]


def test_a_main_stack_still_being_deployed_is_waited_for_quietly(
    cbd, monkeypatch, capsys
):
    """A -iam pre-stack is created before its main stack, so it can pass the age
    gate while the main stack is still CREATE_IN_PROGRESS (which the listing
    omits). That run is healthy: the role is kept and nothing is warned."""
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    fake = _install(
        cbd,
        monkeypatch,
        [_summary("idp-0709-211927-iam", old)],
        statuses={"idp-0709-211927": "CREATE_IN_PROGRESS"},
    )
    assert cbd.cleanup_stale_idp_stacks() == []
    assert fake.deleted == []
    out = capsys.readouterr().out
    assert "⚠️" not in out
    assert "deferred: its main stack idp-0709-211927 is CREATE_IN_PROGRESS" in out


def test_a_complete_main_stack_inside_the_age_gate_is_not_called_wedged(
    cbd, monkeypatch, capsys
):
    """The -iam pre-stack is created first, so it can clear the age gate while
    its main stack, CREATE_COMPLETE and listed, has not. A later run's age gate
    deletes that stack; nobody is asked to."""
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    young = _summary("idp-0709-211927", cbd.IDP_STACK_STALE_AGE_SECONDS - 60)
    fake = _install(cbd, monkeypatch, [young, _summary("idp-0709-211927-iam", old)])
    abandoned = []
    assert cbd.cleanup_stale_idp_stacks(abandoned=abandoned) == []
    assert fake.deleted == []
    assert abandoned == []
    assert "⚠️" not in capsys.readouterr().out


def test_a_main_stack_in_review_in_progress_is_warned(cbd, monkeypatch, capsys):
    """REVIEW_IN_PROGRESS is the one *_IN_PROGRESS state that never moves."""
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    _install(
        cbd,
        monkeypatch,
        [_summary("idp-0709-211927-iam", old)],
        statuses={"idp-0709-211927": "REVIEW_IN_PROGRESS"},
    )
    abandoned = []
    cbd.cleanup_stale_idp_stacks(abandoned=abandoned)
    assert "REVIEW_IN_PROGRESS, which waiting will not resolve" in (
        capsys.readouterr().out
    )
    assert len(abandoned) == 1


def test_a_wedged_main_stack_the_listing_omits_is_warned(cbd, monkeypatch, capsys):
    """UPDATE_ROLLBACK_FAILED is left out of the listing and never converges on
    its own; the -iam stack is kept and the state is named."""
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    fake = _install(
        cbd,
        monkeypatch,
        [_summary("idp-0709-211927-iam", old)],
        statuses={"idp-0709-211927": "UPDATE_ROLLBACK_FAILED"},
    )
    abandoned = []
    assert cbd.cleanup_stale_idp_stacks(abandoned=abandoned) == []
    assert fake.deleted == []
    out = capsys.readouterr().out
    assert "idp-0709-211927-iam kept" in out
    assert "UPDATE_ROLLBACK_FAILED" in out
    # Said again in the run's summary, since nobody reads every ⚠️ in a log.
    assert len(abandoned) == 1 and "UPDATE_ROLLBACK_FAILED" in abandoned[0]


def test_a_failed_main_stack_not_retried_is_not_called_retried(
    cbd, monkeypatch, capsys
):
    """ "Retried this run" must mean a delete was issued: a DELETE_FAILED main
    stack still inside the age gate is not deleted, so its -iam stack (created
    first, and stale) is held by a stack waiting will not resolve."""
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    young = _summary("idp-0709-211927", cbd.IDP_STACK_STALE_AGE_SECONDS - 60)
    young["StackStatus"] = "DELETE_FAILED"
    fake = _install(
        cbd,
        monkeypatch,
        [young, _summary("idp-0709-211927-iam", old)],
        statuses={"idp-0709-211927": "DELETE_FAILED"},
    )
    abandoned = []
    assert cbd.cleanup_stale_idp_stacks(abandoned=abandoned) == []
    assert fake.deleted == []
    out = capsys.readouterr().out
    assert "retried this run" not in out
    assert "DELETE_FAILED, which waiting will not resolve" in out
    assert len(abandoned) == 1


def test_a_main_stack_still_deleting_is_not_a_finding(cbd, monkeypatch):
    old = cbd.IDP_STACK_STALE_AGE_SECONDS + 3600
    _install(
        cbd,
        monkeypatch,
        [_summary("idp-0709-211927", old), _summary("idp-0709-211927-iam", old)],
    )
    assert cbd.cleanup_stale_idp_stacks() == []
