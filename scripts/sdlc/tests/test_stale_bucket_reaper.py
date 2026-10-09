"""Unit tests for cleanup_stale_idp_buckets — the S3-bucket-leak startup reaper.

Buckets leak independently of stacks: CloudFormation can't delete a non-empty
bucket, so an interrupted `idp-cli delete` leaves the bucket behind even after
the stack is gone (thousands accumulated this way). This reaper is destructive,
so these mock-boto3 tests pin the SAFETY logic: never delete a bucket whose run
still has ANY CloudFormation stack (protected), never delete one younger than
the age gate, always empty versions before deleting, and only touch idp- names.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

#: The SDLC pipeline template carrying the CodeBuild role's grants.
PIPELINE_TEMPLATE = Path(__file__).resolve().parents[1] / "cfn" / "codepipeline-s3.yml"


class _FakeStackPaginator:
    def __init__(self, stack_names):
        self._names = stack_names

    def paginate(self, **kwargs):
        return [{"StackSummaries": [{"StackName": n} for n in self._names]}]


class _FakeS3Client:
    def __init__(self, buckets, live_stacks):
        # buckets: list of (name, age_seconds)
        self._buckets = buckets
        self._live_stacks = live_stacks
        self.deleted = []

    # used by _live_idp_run_prefixes
    def get_paginator(self, name):
        assert name == "list_stacks"
        return _FakeStackPaginator(self._live_stacks)

    def list_buckets(self):
        now = datetime.now(tz=timezone.utc)
        return {
            "Buckets": [
                {"Name": n, "CreationDate": now - timedelta(seconds=age)}
                for n, age in self._buckets
            ]
        }

    def delete_bucket(self, Bucket):
        self.deleted.append(Bucket)


class _FakeBucket:
    def __init__(self, name, emptied):
        self._name = name
        self._emptied = emptied

    @property
    def object_versions(self):
        parent = self

        class _OV:
            def delete(self):
                parent._emptied.append(parent._name)

        return _OV()


class _FakeS3Resource:
    def __init__(self):
        self.emptied = []

    def Bucket(self, name):
        return _FakeBucket(name, self.emptied)


def _install(cbd, monkeypatch, buckets, live_stacks):
    client = _FakeS3Client(buckets, live_stacks)
    resource = _FakeS3Resource()

    def fake_client(name, *a, **k):
        # both cloudformation (for _live_idp_run_prefixes) and s3 route to the
        # same fake — get_paginator/list_buckets/delete_bucket don't collide.
        return client

    monkeypatch.setattr(cbd.boto3, "client", fake_client)
    monkeypatch.setattr(cbd.boto3, "resource", lambda name, *a, **k: resource)
    return client, resource


def test_reaps_old_bucket_with_no_live_stack(cbd, monkeypatch):
    old = cbd.IDP_BUCKET_STALE_AGE_SECONDS + 3600
    client, resource = _install(
        cbd,
        monkeypatch,
        buckets=[("idp-0709-160356-inputbucket-abc", old)],
        live_stacks=[],  # no stacks at all → nothing protected
    )
    cbd.cleanup_stale_idp_buckets()
    assert client.deleted == ["idp-0709-160356-inputbucket-abc"]
    # must empty versions BEFORE deleting
    assert resource.emptied == ["idp-0709-160356-inputbucket-abc"]


def test_protects_bucket_whose_run_has_a_live_stack(cbd, monkeypatch):
    old = cbd.IDP_BUCKET_STALE_AGE_SECONDS + 3600
    client, _ = _install(
        cbd,
        monkeypatch,
        buckets=[("idp-0716-165247-outputbucket-xyz", old)],
        # the run's primary stack still exists → protect ALL its buckets
        live_stacks=["idp-0716-165247", "idp-0716-165247-headless-iam"],
    )
    cbd.cleanup_stale_idp_buckets()
    assert client.deleted == []


def test_skips_young_bucket_even_without_stack(cbd, monkeypatch):
    young = cbd.IDP_BUCKET_STALE_AGE_SECONDS - 600
    client, _ = _install(
        cbd,
        monkeypatch,
        buckets=[("idp-0716-170000-inputbucket-new", young)],
        live_stacks=[],
    )
    cbd.cleanup_stale_idp_buckets()
    # age gate is the backstop for a brand-new bucket whose stack hasn't
    # registered yet — must not delete it.
    assert client.deleted == []


def test_ignores_non_idp_buckets(cbd, monkeypatch):
    old = cbd.IDP_BUCKET_STALE_AGE_SECONDS + 3600
    client, _ = _install(
        cbd,
        monkeypatch,
        buckets=[
            ("some-other-bucket", old),
            ("genaiic-sdlc-sourcecode-020432867916-us-east-1", old),
            ("idp-0709-160356-inputbucket-abc", old),
        ],
        live_stacks=[],
    )
    cbd.cleanup_stale_idp_buckets()
    assert client.deleted == ["idp-0709-160356-inputbucket-abc"]


def test_mixed_batch_only_deletes_safe_ones(cbd, monkeypatch):
    old = cbd.IDP_BUCKET_STALE_AGE_SECONDS + 3600
    young = cbd.IDP_BUCKET_STALE_AGE_SECONDS - 600
    client, _ = _install(
        cbd,
        monkeypatch,
        buckets=[
            ("idp-0709-160356-inputbucket-a", old),  # delete
            ("idp-0716-165247-inputbucket-b", old),  # protected (live stack)
            ("idp-0716-170000-inputbucket-c", young),  # too young
            ("idp-0705-120000-outputbucket-d", old),  # delete
        ],
        live_stacks=["idp-0716-165247"],
    )
    cbd.cleanup_stale_idp_buckets()
    assert set(client.deleted) == {
        "idp-0709-160356-inputbucket-a",
        "idp-0705-120000-outputbucket-d",
    }


def test_delete_error_does_not_abort_batch(cbd, monkeypatch):
    old = cbd.IDP_BUCKET_STALE_AGE_SECONDS + 3600
    client, _ = _install(
        cbd,
        monkeypatch,
        buckets=[
            ("idp-0709-100000-inputbucket-a", old),
            ("idp-0709-200000-inputbucket-b", old),
        ],
        live_stacks=[],
    )
    orig = client.delete_bucket

    def flaky(Bucket):
        if Bucket.endswith("-a"):
            raise RuntimeError("BucketNotEmpty")
        orig(Bucket)

    monkeypatch.setattr(client, "delete_bucket", flaky)
    cbd.cleanup_stale_idp_buckets()
    # the second bucket still gets deleted despite the first raising
    assert client.deleted == ["idp-0709-200000-inputbucket-b"]


def test_never_raises_on_api_error(cbd, monkeypatch):
    class _Boom:
        def get_paginator(self, name):
            raise RuntimeError("throttled")

        def list_buckets(self):
            raise RuntimeError("throttled")

    monkeypatch.setattr(cbd.boto3, "client", lambda name, *a, **k: _Boom())
    monkeypatch.setattr(cbd.boto3, "resource", lambda name, *a, **k: object())
    cbd.cleanup_stale_idp_buckets()  # must swallow, not raise


# ---------------------------------------------------------------------------
# The permission the reaper cannot work without
#
# ⚠️ `cleanup_stale_idp_buckets` calls `list_buckets` FIRST, and if that is
# denied the reaper logs one line and returns having reaped nothing. Every test
# above mocks boto3, so all of them pass against a role that cannot make the
# call at all — which is exactly what happened: the grant was missing, the
# reaper did nothing on every run for months, and the only symptom was `idp-`
# buckets accumulating (14 of them, the oldest three months old, when this was
# finally measured).
#
# `s3:ListAllMyBuckets` is an account-level operation with no resource to scope
# to, so it needs `Resource: '*'`. The role's other S3 grants are bucket-scoped
# — including an `s3:*` — and a bucket-scoped wildcard cannot cover it. Pinning
# the grant is the only offline check that the reaper is able to run.
# ---------------------------------------------------------------------------

#: The role the CodeBuild project runs as, and so the only one whose grants
#: decide whether the reaper can make its call. That is a premise about
#: `ServiceRole` on the project, not about this constant, so the test asserts it
#: rather than assuming it -- repointing `ServiceRole` at the supplied-role
#: parameter denies the reaper again while every grant named here is untouched.
REAPER_ROLE = "CodeBuildRole"
REAPER_ACTION = "s3:ListAllMyBuckets"

#: Accepted in place of the specific action: a consolidated `s3:*` still grants
#: it. Without this, narrowing *or widening* the statement both fail, and the
#: widening case would report "no longer granted s3:ListAllMyBuckets" about a
#: policy that grants it — sending the reader after the wrong thing.
REAPER_ACTION_EQUIVALENTS = frozenset({REAPER_ACTION, "s3:*"})


def _as_list(value):
    """CloudFormation accepts a scalar wherever it accepts a list of them."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _statements_for_role(template: dict, role: str) -> list[dict]:
    """Every IAM statement attached to `role`, by either of the two shapes.

    A grant can reach a role inline (`AWS::IAM::Role.Properties.Policies`) or by
    a separate `AWS::IAM::Policy` naming it in `Roles`, and this template uses
    both for the same role. Collecting the two together is the whole point: the
    question is what the role can do, not where somebody wrote it down.
    """
    resources = template.get("Resources") or {}
    statements: list[dict] = []

    role_body = resources.get(role) or {}
    for policy in _as_list((role_body.get("Properties") or {}).get("Policies")):
        statements.extend(
            _as_list((policy.get("PolicyDocument") or {}).get("Statement"))
        )

    for body in resources.values():
        if body.get("Type") != "AWS::IAM::Policy":
            continue
        props = body.get("Properties") or {}
        # `Roles: [!Ref CodeBuildRole]` parses to [{'Ref': 'CodeBuildRole'}].
        attached = {
            entry.get("Ref")
            for entry in _as_list(props.get("Roles"))
            if isinstance(entry, dict)
        }
        if role in attached:
            statements.extend(
                _as_list((props.get("PolicyDocument") or {}).get("Statement"))
            )

    return statements


#: Routes to a role this collector deliberately does not model:
#:
#:   * `Roles: [!Sub '${CodeBuildRole}']` rather than `!Ref` (same value),
#:   * a statement or policy wrapped in `Fn::If`,
#:   * an `AWS::IAM::ManagedPolicy` attached to the role,
#:   * `AWS::IAM::RolePolicy`.
#:
#: Each makes the grant *invisible* here, so the test fails rather than passing
#: -- the safe direction, loud and wrong-way-correct. Written down because the
#: reach of a collector is the thing a reader cannot infer from reading it, and
#: a false failure that names nothing costs an afternoon.
_UNMODELLED_ATTACHMENT_ROUTES = (
    "Fn::If-wrapped policies, !Sub role references, "
    "AWS::IAM::ManagedPolicy, AWS::IAM::RolePolicy"
)


def _referenced_logical_ids(node) -> set[str]:
    """Every logical id a value refers to, through `Ref` or `Fn::GetAtt`.

    ⚠️ **Exact ids, not a substring search.** `ServiceRole` here is an `Fn::If`
    choosing between `!GetAtt CodeBuildRole.Arn` and `!Ref CodeBuildRoleArn`, and
    `CodeBuildRole` is a *prefix* of `CodeBuildRoleArn` -- so asking whether the
    role's name appears anywhere in the stringified value answers yes even when
    the project has been repointed at the supplied-role parameter, which is
    precisely the case this exists to detect. Walking the structure and taking
    the ids whole is the only way to tell those two apart.
    """
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Ref" and isinstance(value, str):
                found.add(value)
            elif key == "Fn::GetAtt":
                parts = value if isinstance(value, list) else str(value).split(".")
                if parts:
                    found.add(str(parts[0]))
            else:
                found |= _referenced_logical_ids(value)
    elif isinstance(node, list):
        for item in node:
            found |= _referenced_logical_ids(item)
    return found


def _runs_as_role(template: dict, role: str) -> bool:
    """Whether any CodeBuild project in `template` runs as `role`.

    The premise behind pinning one role's grants. `Fn::If` is not evaluated --
    a project that runs as this role on *either* branch counts, because the
    question is whether the role is still in play at all.
    """
    return any(
        role
        in _referenced_logical_ids((body.get("Properties") or {}).get("ServiceRole"))
        for body in (template.get("Resources") or {}).values()
        if body.get("Type") == "AWS::CodeBuild::Project"
    )


def test_the_codebuild_role_can_list_the_accounts_buckets():
    """The reaper's first call must be permitted *to its own role*, on `Resource: '*'`.

    Parsed rather than matched against the template text, and both halves of
    that matter. A text match cannot tell which role carries a statement, so it
    reports success with the grant sitting on a different role while the reaper's
    role has lost it -- which would make the only protection for this fix a gate
    with exactly the defect the fix exists to remove. And a text match pins YAML
    *formatting*: reordering `Action` and `Resource`, or adding a second action
    to the statement, changes no permission and must not fail.

    `load_template` comes from the sibling module in this directory, which
    `conftest.py` puts on `sys.path` for this purpose; it converts the CFN
    short-form tags, so `!Ref`/`!Sub` arrive as plain dicts.
    """
    from test_iam_trust_policy_partitions import load_template

    template = load_template(PIPELINE_TEMPLATE)

    # The premise first: pinning this role's grants says nothing unless the
    # CodeBuild project still runs as it. Repointing `ServiceRole` at the
    # supplied-role parameter denies the reaper while leaving every grant below
    # exactly as it is, so without this the test passes and the reaper is broken.
    assert REAPER_ROLE in (template.get("Resources") or {}), (
        f"{PIPELINE_TEMPLATE.name} declares no {REAPER_ROLE} resource. Everything "
        f"below is keyed on that logical id, and the grants would still be found "
        f"through a dangling `!Ref` in a policy, so this is checked first."
    )
    assert _runs_as_role(template, REAPER_ROLE), (
        f"no AWS::CodeBuild::Project in {PIPELINE_TEMPLATE.name} runs as "
        f"{REAPER_ROLE}, so pinning that role's grants says nothing about whether "
        f"the reaper can list the account"
    )

    statements = _statements_for_role(template, REAPER_ROLE)
    assert statements, (
        f"no IAM statements found for {REAPER_ROLE} in {PIPELINE_TEMPLATE.name} — "
        f"the role was renamed or the template restructured, and every assertion "
        f"below would pass vacuously"
    )

    granting = [
        statement
        for statement in statements
        if statement.get("Effect") == "Allow"
        and REAPER_ACTION_EQUIVALENTS & set(_as_list(statement.get("Action")))
    ]
    assert granting, (
        f"{REAPER_ROLE} is no longer granted {REAPER_ACTION} (or `s3:*`) in "
        f"{PIPELINE_TEMPLATE.name}. cleanup_stale_idp_buckets calls list_buckets "
        f"as its first action and swallows the AccessDenied, so removing this "
        f"makes the bucket reaper a no-op that still reports success. If the "
        f"grant IS there, check how it reaches the role: this collector reads "
        f"inline `Policies` and an `AWS::IAM::Policy` naming the role via `!Ref`, "
        f"and does not model {_UNMODELLED_ATTACHMENT_ROUTES}."
    )

    assert any("*" in _as_list(statement.get("Resource")) for statement in granting), (
        f"{REAPER_ACTION} is granted to {REAPER_ROLE} but not on `Resource: '*'`. "
        f"It is an account-level operation with no resource to scope to, so any "
        f"ARN — even an `s3:*` on a bucket — evaluates to implicitDeny."
    )
