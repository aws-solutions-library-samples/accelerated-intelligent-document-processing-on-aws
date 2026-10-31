# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every Step 0 startup sweep must report whether it actually ran (#1434).

All six `cleanup_stale_*` reapers in `codebuild_deployment.py` shared one shape:
a single top-level `except Exception` whose body was one `print`, no return
value, and a bare call in `main()`. So a run whose enumeration was refused
considered ZERO targets, established nothing about the account, printed one `⚠️`
line into a six-figure-line build log, and ended green — indistinguishable from
a run that enumerated and found nothing to reap. #1433 removed that shape from
the bucket sweep; this module is the same removal asserted for the class.

**Parametrised off `STARTUP_SWEEPS`, not off a list written here.** The
registry in the module under test is the source of the membership, and
`test_every_cleanup_function_in_the_module_is_registered` derives the universe
from the module's own source, so a seventh reaper added later is either covered
by everything below or fails in a named way. The per-sweep *driving* data
(`_SWEEP_SPECS`) is unavoidably per-sweep — they call different APIs — and a
registered sweep with no spec fails rather than being quietly skipped.

Three directions per sweep, and the middle one is the direction a careless fix
breaks:

* an enumeration failing is a finding — asserted on the returned OUTCOME, never
  on a printed string, because a printed string is the thing that was already
  there and that nobody read. A sweep with two enumerations gets a case per
  enumeration, since a test that only fails the second misses half the surface;
* enumerating and finding nothing is still the quiet path with NO finding. A
  reaper that reports a problem on every green deploy trains the reader to ignore
  the row, which is how the warning it replaces came to be ignored;
* one resource that would not delete stays best effort — named, and not a
  finding, because one stuck resource must not cost a night's deploy.

And the decision that is per member rather than per class: `fails_run`. Two of
the six report without ending the run red, for two different reasons recorded in
the registry, and both directions are pinned — a sweep marked fatal must flip the
verdict, and a sweep marked non-fatal must not.

What this module does NOT pin, so a green run is not over-read: live behaviour.
Every fake here is a stand-in, so none of this says the deployed CodeBuild role
can make the calls. `test_stale_bucket_reaper.py` pins the one grant that is
checkable offline (`s3:ListAllMyBuckets`); the equivalent question for the other
five sweeps' calls needs account access no in-tree gate has.

The precedence of `main()`'s one-line `failure_reason` is also unpinned, as it
was in #1433 — but it is no longer *mutable* in the way that noted: the four
fatal sweeps spell it `failure_reason = failure_reason or reason`, which has no
guard to invert, where the shape #1433 left behind was `if not failure_reason:`.
"""

import ast
import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import botocore.session
import pytest
from botocore.exceptions import ClientError

pytestmark = pytest.mark.unit

HARNESS = Path(__file__).resolve().parents[1] / "codebuild_deployment.py"

#: Older than every age gate in the module (the widest is the 30-day diagnostics
#: retention window), so one constant serves all six sweeps.
STALE_AGE_SECONDS = 31 * 24 * 3600

#: A run prefix whose stacks are all gone, so nothing protects its leftovers.
DEAD_RUN = "idp-0101-000000"

#: A second run prefix with no stack standing, so a case can show the BDA sweep's
#: finding does not multiply with the runs it listed.
OTHER_RUN = "idp-0202-000000"


def _client_error(code, message, operation="ListStacks"):
    """A REAL botocore `ClientError` carrying a real error code.

    Real rather than a stand-in, so a finding built from it reads the way one
    built from the account's answer does: `str()` of a real `ClientError` names
    the code and the operation, which is what tells a reader a throttle from a
    denial, and a double raising a bare `Exception` carries neither.
    """
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


#: What CloudFormation answers when asked BY NAME about a stack that is not
#: there. The code is stated by the `DescribeStacks` API reference ("If the stack
#: doesn't exist, a ValidationError is returned"); the message wording is not in
#: the API reference, and is the shape moto reproduces (`moto/cloudformation/
#: exceptions.py`). The BDA sweep asks nothing by name, so here it is the fake's
#: answer to a question no case expects, and an error the stack enumeration can
#: raise that must still read as a listing that failed.
STACK_GONE = ("ValidationError", f"Stack with id {DEAD_RUN} does not exist")


# ---------------------------------------------------------------------------
# One fake AWS surface for all six sweeps
#
# ⚠️ A double that cannot fail the enumeration call makes every assertion below
# vacuous. Failure is therefore expressed the way the API expresses it — the call
# raises — and `failing` names which call does, so the same object drives the
# clean, the enumeration-failure and the per-item-failure cases.
#
# The method names the six sweeps use do not collide, so one object can stand in
# for every service `boto3.client` is asked for, which is also what
# test_stale_bucket_reaper.py's fake does.
# ---------------------------------------------------------------------------


class _FakePaginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        return self._pages


class _StackListing:
    """`list_stacks` as CloudFormation pages it, honouring `StackStatusFilter`.

    Read from `_FakeAws.stacks`, one stack to a page so a listing that fails
    part-way can be expressed. Two properties of the real call are what the BDA
    sweep's status cases turn on, so neither is left to a double that ignores
    its arguments the way `_FakePaginator` does: a deleted stack is still listed,
    as `DELETE_COMPLETE`, by a call with no filter, and a status the filter does
    not name is a stack the caller never sees.
    """

    def __init__(self, aws):
        self._aws = aws

    def paginate(self, StackStatusFilter=None):  # noqa: N803 - boto3 casing
        pages = [
            {"StackSummaries": [{"StackName": name, "StackStatus": status}]}
            for name, status in self._aws.stacks.items()
            if StackStatusFilter is None or status in StackStatusFilter
        ]
        answered, error = self._aws.stack_listing_error or (len(pages), None)
        yield from pages[:answered]
        if error is not None:
            raise error


class _FakeAws:
    """Account contents plus a chosen failure, standing in for every client.

    `stacks` is `{StackName: StackStatus}`, the account's stacks with deleted
    ones included as `DELETE_COMPLETE`, which `list_stacks` then pages through
    `_StackListing`. None leaves `list_stacks` to `contents`, where the other
    sweeps' cases write its pages directly.

    `stack_listing_error` is `(pages, exception)`: the stack listing raises
    `exception` once it has answered `pages` pages, so 0 fails it outright. A
    different surface from `failing`, which refuses the paginator itself with
    one error.

    `calls` records every enumeration call, in the order the sweep made it.
    """

    def __init__(
        self,
        contents=None,
        failing=None,
        breaking=None,
        stacks=None,
        stack_listing_error=None,
    ):
        self.contents = contents or {}
        self.failing = failing
        self.breaking = breaking
        self.stacks = stacks
        self.stack_listing_error = stack_listing_error
        self.calls = []
        self.deleted = []
        self.described = []

    def _check(self, api):
        self.calls.append(api)
        if self.failing == api:
            raise RuntimeError(f"AccessDenied: {api}")

    # --- enumerations -----------------------------------------------------
    def get_paginator(self, api):
        self._check(api)
        if api == "list_stacks" and self.stacks is not None:
            return _StackListing(self)
        return _FakePaginator(self.contents.get(api, [{}]))

    def list_data_automation_projects(self):
        self._check("list_data_automation_projects")
        return {"projects": self.contents.get("projects", [])}

    def list_buckets(self):
        self._check("list_buckets")
        return {"Buckets": self.contents.get("buckets", [])}

    def describe_stacks(self, StackName):  # noqa: N803 - boto3 casing
        """CloudFormation's answer BY NAME, recorded for the cases that expect none.

        A deleted stack is not described by name: CloudFormation raises the
        error a stack that never existed raises.
        """
        self._check("describe_stacks")
        self.described.append(StackName)
        status = (self.stacks or {}).get(StackName, "DELETE_COMPLETE")
        if status == "DELETE_COMPLETE":
            raise _client_error(
                STACK_GONE[0],
                f"Stack with id {StackName} does not exist",
                "DescribeStacks",
            )
        return {"Stacks": [{"StackName": StackName, "StackStatus": status}]}

    # --- per-item actions -------------------------------------------------
    def _delete(self, api, name):
        if self.breaking == api:
            raise RuntimeError(f"{api} refused for {name}")
        self.deleted.append(name)

    def delete_blueprint(self, blueprintArn, blueprintVersion=None):  # noqa: N803
        if blueprintVersion is not None:
            # The sweep's own version-delete attempt, swallowed by design.
            return None
        self._delete("delete_blueprint", blueprintArn)
        return None

    def delete_data_automation_project(self, projectArn):  # noqa: N803
        self._delete("delete_data_automation_project", projectArn)

    def delete_stack(self, StackName):  # noqa: N803
        self._delete("delete_stack", StackName)

    def delete_bucket(self, Bucket):  # noqa: N803
        self._delete("delete_bucket", Bucket)

    def delete_object(self, Bucket, Key):  # noqa: N803
        self._delete("delete_object", Key)


class _FakeS3Resource:
    """`boto3.resource("s3")`, for the bucket sweep's empty-then-delete."""

    def __init__(self):
        self.emptied = []

    def Bucket(self, name):  # noqa: N802 - boto3 casing
        emptied = self.emptied

        class _Bucket:
            @property
            def object_versions(self):
                class _Versions:
                    def delete(self):
                        emptied.append(name)

                return _Versions()

        return _Bucket()


def _contents(cbd, sweep, with_target):
    """What the account holds so `sweep` has exactly one reapable target.

    Per sweep, because each enumerates something different. `with_target=False`
    is the clean case: every listing answers, and holds nothing this sweep wants.
    """
    now = datetime.now(tz=timezone.utc)
    old = now - timedelta(seconds=STALE_AGE_SECONDS)

    if sweep == "cleanup_stale_bda_blueprints":
        if not with_target:
            return {"list_blueprints": [{"blueprints": []}], "projects": []}
        return {
            "list_blueprints": [
                {
                    "blueprints": [
                        {
                            "blueprintName": DEAD_RUN,
                            "blueprintArn": f"arn:fake:blueprint/{DEAD_RUN}",
                        }
                    ]
                }
            ],
            "projects": [],
            "list_stacks": [{"StackSummaries": []}],
        }

    if sweep == "cleanup_stale_apigw_test_vpcs":
        names = [f"{DEAD_RUN}-apigw-vpc"] if with_target else []
        return {
            "list_stacks": [
                {
                    "StackSummaries": [
                        {"StackName": n, "CreationTime": old} for n in names
                    ]
                }
            ]
        }

    if sweep == "cleanup_stale_idp_stacks":
        names = [DEAD_RUN] if with_target else []
        return {
            "list_stacks": [
                {
                    "StackSummaries": [
                        {"StackName": n, "CreationTime": old} for n in names
                    ]
                }
            ]
        }

    if sweep == "cleanup_stale_idp_buckets":
        # No stacks at all, so nothing protects the bucket's run prefix.
        return {
            "list_stacks": [{"StackSummaries": []}],
            "buckets": (
                [{"Name": f"{DEAD_RUN}-inputbucket-abc", "CreationDate": old}]
                if with_target
                else []
            ),
        }

    if sweep == "cleanup_stale_idp_roles":
        # No stacks at all, so nothing protects the role's run prefix. These are
        # leaked by `DeletionPolicy: Retain` rather than by a failed teardown, so
        # the realistic target is a role whose stack is long gone.
        roles = (
            [
                {
                    "RoleName": f"{DEAD_RUN}-DOCUMENTK-StartIngestionJobFunction-abc",
                    "CreateDate": old,
                }
            ]
            if with_target
            else []
        )
        return {
            "list_stacks": [{"StackSummaries": []}],
            "list_roles": [{"Roles": roles}],
        }

    if sweep == "cleanup_stale_idp_policies":
        policies = (
            [
                {
                    "PolicyName": f"{DEAD_RUN}-PermissionsBoundary",
                    "Arn": f"arn:fake:policy/{DEAD_RUN}-PermissionsBoundary",
                    "CreateDate": old,
                }
            ]
            if with_target
            else []
        )
        return {
            "list_stacks": [{"StackSummaries": []}],
            "list_policies": [{"Policies": policies}],
        }

    if sweep == "cleanup_stale_ci_diagnostics":
        objects = (
            # Keyed by build AND stack, the shape persist_diagnostics_bundle writes.
            [
                {
                    "Key": f"{cbd.DIAGNOSTICS_S3_PREFIX}/build-1/{DEAD_RUN}/evidence.json",
                    "LastModified": old,
                }
            ]
            if with_target
            else []
        )
        return {"list_objects_v2": [{"Contents": objects}]}

    raise AssertionError(f"no account contents written for {sweep}")


#: Per sweep: the enumerations whose failure leaves it with nothing to sweep (one
#: parametrised case each), the per-item call whose failure must stay best
#: effort, and the line its quiet path prints.
#:
#: ⚠️ `enumerations` is the part to get right. The bucket and policy sweeps each
#: make TWO listings, and the protected-prefix listing runs FIRST — without it
#: there is no safe selection to make — so a case that only fails the second
#: covers half the surface. The BDA sweep makes THREE where a name it lists
#: carries a run prefix, as its `_contents` target does, and reads the same
#: protected-prefix listing LAST, for the reason its docstring gives. Every
#: registered sweep needs an entry here; one without is a failure, not a skip.
#:
#: `age_gate` drives the age-gate direction (#1445): `field` is the timestamp the
#: listing carries, `young_line` a fragment the sweep prints for an item inside
#: the gate, and its presence says the sweep HAS an age gate at all. Five of the
#: six do; `None` for the BDA sweep, which gates on stack liveness instead, and
#: `test_every_sweep_with_an_age_gate_in_the_module_has_one_here` derives that
#: split from the module's source rather than trusting this dict, so a sweep that
#: grows a gate later is covered or fails by name.
_SWEEP_SPECS = {
    "cleanup_stale_bda_blueprints": {
        "enumerations": (
            "list_blueprints",
            "list_data_automation_projects",
            "list_stacks",
        ),
        "per_item": "delete_blueprint",
        "quiet_line": "✅ No stale BDA resources found",
        "age_gate": None,  # gated on stack liveness, not on an age
    },
    "cleanup_stale_apigw_test_vpcs": {
        "enumerations": ("list_stacks",),
        "per_item": None,  # delete_apigw_test_vpc returns False; see below
        "quiet_line": "✅ No stale apigw test VPC stacks to reap",
        "age_gate": {"field": "CreationTime", "young_line": "skipping — only"},
    },
    "cleanup_stale_idp_stacks": {
        "enumerations": ("list_stacks",),
        "per_item": "delete_stack",
        "quiet_line": "✅ No stale idp- test stacks to reap",
        "age_gate": {
            "field": "CreationTime",
            "young_line": "young idp- stack(s) skipped",
        },
    },
    "cleanup_stale_idp_buckets": {
        "enumerations": ("list_stacks", "list_buckets"),
        "per_item": "delete_bucket",
        "quiet_line": "✅ No stale idp- test buckets to reap",
        "age_gate": {
            "field": "CreationDate",
            "young_line": "younger than the age gate",
        },
    },
    "cleanup_stale_idp_roles": {
        "enumerations": ("list_stacks", "list_roles"),
        "per_item": None,  # _delete_iam_role reports (False, detail)
        "quiet_line": "✅ No leaked idp- IAM roles to reap",
        "age_gate": {"field": "CreateDate", "young_line": "younger than the age gate"},
    },
    "cleanup_stale_idp_policies": {
        "enumerations": ("list_stacks", "list_policies"),
        "per_item": None,  # _delete_managed_policy reports (False, detail)
        "quiet_line": "✅ No orphaned idp- IAM managed policies to reap",
        "age_gate": {"field": "CreateDate", "young_line": "younger than the age gate"},
    },
    "cleanup_stale_ci_diagnostics": {
        "enumerations": ("list_objects_v2",),
        "per_item": "delete_object",
        "quiet_line": "✅ No expired diagnostics bundles",
        # Its gate runs the other way round — past the window is what deletes —
        # and nothing is printed for an object inside it, since every bundle
        # written in the last 30 days is inside it on every run.
        "age_gate": {"field": "LastModified", "young_line": None},
    },
}


def _install(
    cbd,
    monkeypatch,
    sweep,
    *,
    with_target,
    failing=None,
    breaking=None,
    contents=None,
    stacks=None,
    stack_listing_error=None,
):
    """Point the harness at the fake account and return it."""
    aws = _FakeAws(
        contents if contents is not None else _contents(cbd, sweep, with_target),
        failing,
        breaking,
        stacks=stacks,
        stack_listing_error=stack_listing_error,
    )
    monkeypatch.setattr(cbd.boto3, "client", lambda *a, **k: aws)
    monkeypatch.setattr(cbd.boto3, "resource", lambda *a, **k: _FakeS3Resource())
    # The BDA sweep paces its deletes; nothing here is waiting on AWS.
    monkeypatch.setattr(cbd.time, "sleep", lambda *_a, **_k: None)

    # Only the diagnostics sweep reads SOURCE_BUCKET as its target store. For the
    # stack sweep an unset value keeps the retention-marker read out of the way,
    # which is a separate handler and a separate claim.
    if sweep == "cleanup_stale_ci_diagnostics":
        monkeypatch.setenv("SOURCE_BUCKET", "a-bucket")
    else:
        monkeypatch.delenv("SOURCE_BUCKET", raising=False)

    # Two sweeps delegate the per-item action to a module function that already
    # swallows its own failures and reports the outcome in its return value.
    # Patched to the SUCCESS answer here; the best-effort direction drives them
    # to the failure answer in its own test. They RECORD as well, so
    # `aws.deleted` answers "did this sweep reap anything" for all six rather
    # than for the four whose delete reaches the fake client — a reap test that
    # cannot see the reap is vacuous in the direction that matters.
    def _reap_vpc(name):
        aws.deleted.append(name)
        return True

    def _reap_policy(_client, arn):
        aws.deleted.append(arn)
        return (True, "")

    def _reap_role(_client, name):
        aws.deleted.append(name)
        return (True, "")

    monkeypatch.setattr(cbd, "delete_apigw_test_vpc", _reap_vpc)
    monkeypatch.setattr(cbd, "_delete_managed_policy", _reap_policy)
    monkeypatch.setattr(cbd, "_delete_iam_role", _reap_role)
    return aws


def _run(cbd, sweep):
    return getattr(cbd, sweep)()


def _sweep_names():
    import codebuild_deployment as module

    return [s.func for s in module.STARTUP_SWEEPS]


# ---------------------------------------------------------------------------
# The registry is closed over the module, so nothing can be added uncovered
# ---------------------------------------------------------------------------


def _module_cleanup_functions():
    """Every top-level `cleanup_stale_*` function in the harness, from source.

    Read from the source rather than from the imported module: the question is
    what a reader of the file would call a startup sweep, and that is the set a
    new one joins.
    """
    tree = ast.parse(HARNESS.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("cleanup_stale_")
    }


def test_every_cleanup_function_in_the_module_is_registered(cbd):
    """Universe closure, in both directions.

    Without this the parametrisation below is only as complete as whoever last
    edited the registry, and a seventh reaper would be reported on by nothing
    while every test here stayed green — which is the shape of the defect this
    whole change is about, one level up.
    """
    registered = set(_sweep_names())
    discovered = _module_cleanup_functions()

    assert discovered - registered == set(), (
        f"{sorted(discovered - registered)} is a Step 0 reaper the module "
        f"defines and STARTUP_SWEEPS does not list, so nothing decides whether "
        f"its failure to complete fails the run and nothing gives it a table row"
    )
    assert registered - discovered == set(), (
        f"STARTUP_SWEEPS lists {sorted(registered - discovered)}, which the "
        f"module no longer defines — the entry is stale and its row would never "
        f"render"
    )
    for name in registered:
        assert callable(getattr(cbd, name, None)), f"{name} is not callable"


def test_every_registered_sweep_has_driving_data_here():
    """A registered sweep with no `_SWEEP_SPECS` entry fails rather than skips.

    A missing spec would make every parametrised case below silently narrower,
    and a test that quietly stops covering a member is worse than one that was
    never written, because the green mark claims the coverage.
    """
    missing = sorted(set(_sweep_names()) - set(_SWEEP_SPECS))
    assert not missing, f"no driving data in _SWEEP_SPECS for {missing}"


def test_the_fails_run_decision_is_per_member_not_per_class(cbd):
    """Each entry carries its OWN reason, and they are not the same reason.

    The defect class this repository's exemption registry exists to prevent is
    one justification attached to a set where the justification is a property of
    individual members. Six entries sharing one sentence would read as a
    considered decision and be exactly that mistake, so the reasons must differ.
    """
    reasons = [s.why for s in cbd.STARTUP_SWEEPS]
    assert all(len(r) > 80 for r in reasons), "a one-liner is not a reason"
    assert len(set(reasons)) == len(reasons), (
        "two STARTUP_SWEEPS entries carry the same `why`, so one reason is "
        "standing in for two members whose consequences differ"
    )
    # Both verdicts are actually exercised by the registry as it stands. If they
    # ever were not, the pinning below would be half vacuous.
    verdicts = {s.fails_run for s in cbd.STARTUP_SWEEPS}
    assert verdicts == {True, False}


# ---------------------------------------------------------------------------
# Direction 1: an enumeration failure is a finding, not a completed sweep
# ---------------------------------------------------------------------------


def _enumeration_cases():
    return [
        (sweep, api)
        for sweep in _sweep_names()
        for api in _SWEEP_SPECS[sweep]["enumerations"]
    ]


@pytest.mark.parametrize(
    ("sweep", "api"), _enumeration_cases(), ids=lambda v: str(v).replace(" ", "")
)
def test_a_failed_enumeration_is_a_finding(cbd, monkeypatch, capsys, sweep, api):
    aws = _install(cbd, monkeypatch, sweep, with_target=True, failing=api)

    findings = _run(cbd, sweep)

    assert findings, (
        f"{sweep} could not {api} — it considered zero targets and established "
        f"nothing about the account — yet returned no finding, so the run cannot "
        f"tell it apart from a sweep that enumerated and found nothing. That is "
        f"the defect itself, not a cosmetic difference"
    )
    assert all(f.startswith(cbd.SWEEP_NOT_COMPLETED) for f in findings), findings
    assert aws.deleted == [], f"{sweep} deleted something after {api} failed"
    # And it must not ALSO claim the clean outcome, which is the line a reader
    # skims for.
    assert _SWEEP_SPECS[sweep]["quiet_line"] not in capsys.readouterr().out


@pytest.mark.parametrize("sweep", _sweep_names())
def test_a_failed_enumeration_still_does_not_raise(cbd, monkeypatch, sweep):
    """Step 0 never raises, and that contract is load-bearing.

    These run before the deploy, the suite and the teardown, so an exception here
    costs every signal the night would have produced. The failure has to reach
    the verdict through the return value instead.
    """
    api = _SWEEP_SPECS[sweep]["enumerations"][0]
    _install(cbd, monkeypatch, sweep, with_target=True, failing=api)
    assert _run(cbd, sweep)  # must return a finding rather than propagate


# ---------------------------------------------------------------------------
# Direction 2: a clean sweep is still the quiet path
#
# ⚠️ The direction a careless fix breaks, and the one a failure-only test passes
# on. Making every green deploy report a cleanup problem is worse than the
# silence it replaces: it trains the reader to ignore the row that matters, which
# is how the `⚠️` line this fix removes came to be ignored.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sweep", _sweep_names())
def test_an_enumerated_sweep_with_nothing_to_do_reports_no_finding(
    cbd, monkeypatch, capsys, sweep
):
    aws = _install(cbd, monkeypatch, sweep, with_target=False)

    findings = _run(cbd, sweep)

    assert findings == [], (
        f"{sweep} enumerated its targets and found nothing to reap, which is a "
        f"COMPLETED sweep, yet reported {findings}"
    )
    assert aws.deleted == []
    out = capsys.readouterr().out
    assert _SWEEP_SPECS[sweep]["quiet_line"] in out
    assert cbd.SWEEP_NOT_COMPLETED not in out


@pytest.mark.parametrize("sweep", _sweep_names())
def test_a_sweep_that_reaped_everything_it_found_reports_no_finding(
    cbd, monkeypatch, sweep
):
    """The other clean direction: targets found, all taken, nothing to report."""
    aws = _install(cbd, monkeypatch, sweep, with_target=True)

    findings = _run(cbd, sweep)

    assert findings == [], f"{sweep} reaped its target and still reported {findings}"
    if _SWEEP_SPECS[sweep]["per_item"]:
        assert aws.deleted, f"{sweep} reported clean without deleting its target"


# ---------------------------------------------------------------------------
# Direction 3: one stuck resource stays best effort
#
# The asymmetry the fix has to preserve. One resource that will not delete must
# not fail a deploy: the loop names it, the others are still reaped, the next run
# tries again. The only thing separating it from an enumeration failure is which
# `except` catches it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sweep", [s for s in _sweep_names() if _SWEEP_SPECS[s]["per_item"]]
)
def test_a_per_item_failure_stays_best_effort(cbd, monkeypatch, capsys, sweep):
    api = _SWEEP_SPECS[sweep]["per_item"]
    _install(cbd, monkeypatch, sweep, with_target=True, breaking=api)

    findings = _run(cbd, sweep)

    assert findings == [], (
        f"{sweep} turned one failed {api} into a finding, which would fail a "
        f"deploy over a single stuck resource the next run would take"
    )
    out = capsys.readouterr().out
    assert cbd.SWEEP_NOT_COMPLETED not in out
    assert DEAD_RUN in out, "the stuck resource is not named anywhere"


def test_a_stuck_apigw_vpc_stack_stays_best_effort(cbd, monkeypatch, capsys):
    """The VPC sweep's per-item action reports through a return value.

    `delete_apigw_test_vpc` swallows its own failures and answers False, so the
    best-effort direction has to be driven there rather than by raising.
    """
    _install(cbd, monkeypatch, "cleanup_stale_apigw_test_vpcs", with_target=True)
    monkeypatch.setattr(cbd, "delete_apigw_test_vpc", lambda _name: False)

    findings = cbd.cleanup_stale_apigw_test_vpcs()

    assert findings == []
    out = capsys.readouterr().out
    assert "left stuck" in out and DEAD_RUN in out
    assert cbd.SWEEP_NOT_COMPLETED not in out


def test_a_policy_that_will_not_delete_stays_best_effort(cbd, monkeypatch, capsys):
    """Same shape: `_delete_managed_policy` answers `(False, detail)`."""
    _install(cbd, monkeypatch, "cleanup_stale_idp_policies", with_target=True)
    monkeypatch.setattr(cbd, "_delete_managed_policy", lambda _c, _a: (False, "held"))

    findings = cbd.cleanup_stale_idp_policies()

    assert findings == []
    out = capsys.readouterr().out
    assert "NOT deleted" in out and DEAD_RUN in out
    assert cbd.SWEEP_NOT_COMPLETED not in out


# ---------------------------------------------------------------------------
# Direction 4: an age gate fails CLOSED, in its own counter (#1445)
#
# Three of these gates read an unestablishable age as "old enough to delete" and
# a fourth read it as "the age gate cannot be satisfied" — four siblings
# arguing the same question to opposite answers. The trigger is a listing that
# omits `CreationTime`/`CreationDate`, fields documented as always present, so
# what is asserted here is the DIRECTION the gate takes when it cannot tell,
# not a path anyone has observed.
#
# ⚠️ Three cases per sweep, and all three are needed. Keeping the unknown-age
# item is worthless if the gate stops reaping anything (the reaper goes inert,
# which is the defect #1434 was about from the other side), and it is worth
# little if "age unknown" is reported as "younger than the gate" — the second is
# routine and needs no attention, which is why the policy reaper gave the first
# its own counter.
#
# Parametrised off the registry, with the per-sweep timestamp field as driving
# data, because five near-copies is how one of them ends up subtly different —
# which is the state this change found the module in.
# ---------------------------------------------------------------------------

#: Inside every gate in the module. 1 second rather than a fraction of the
#: narrowest gate, so a gate tuned down does not quietly make this case the
#: unknown-age one.
YOUNG_AGE_SECONDS = 1

#: The ways a timestamp can fail to establish an age, all three of which a
#: listing could carry. A naive datetime is the one that used to raise, which
#: inside a sweep's blanket `except` cost the whole sweep rather than one item.
UNDATEABLE = [
    pytest.param(None, id="absent"),
    pytest.param("2026-01-01T00:00:00Z", id="a-string-not-a-datetime"),
    pytest.param(datetime(2026, 1, 1), id="naive-no-tzinfo"),  # noqa: DTZ001
]


def _reaged(contents, field, value):
    """`contents` with every `field` timestamp replaced by `value`.

    Recursive and by FIELD NAME rather than by position, so it does not depend
    on the shape of each sweep's account contents — the five listings nest
    differently and a positional rewrite would silently miss one, leaving a case
    that drives the old timestamp and passes either way.
    """
    if isinstance(contents, dict):
        return {
            key: (value if key == field else _reaged(sub, field, value))
            for key, sub in contents.items()
        }
    if isinstance(contents, list):
        return [_reaged(sub, field, value) for sub in contents]
    return contents


def _age_gate_sweeps():
    return [s for s in _sweep_names() if _SWEEP_SPECS[s]["age_gate"]]


def _aged(cbd, sweep, value):
    """This sweep's account contents, holding one candidate at `value`."""
    field = _SWEEP_SPECS[sweep]["age_gate"]["field"]
    return _reaged(_contents(cbd, sweep, True), field, value)  # noqa: FBT003


def test_every_sweep_with_an_age_gate_in_the_module_has_one_here():
    """Universe closure for the gate set, derived rather than listed.

    `_listing_age_seconds` is the one age computation behind all of them, so
    reachability from a sweep is what "has an age gate" means — asserted over
    the call graph, because the policy sweep reaches it through
    `_policy_age_seconds` and a direct-call search would miss that one. A sweep
    that grows a gate later is covered by the three cases below or fails here by
    name; one whose gate is removed fails too, since its driving data would
    then assert nothing.
    """
    functions = _top_level_functions(ast.parse(HARNESS.read_text(encoding="utf-8")))

    def _reaches_the_age_helper(name, seen=None):
        seen = seen or set()
        if name in seen or name not in functions:
            return False
        seen.add(name)
        callees = _called_function_names(functions[name])
        return "_listing_age_seconds" in callees or any(
            _reaches_the_age_helper(callee, seen) for callee in sorted(callees)
        )

    gated = {s for s in _sweep_names() if _reaches_the_age_helper(s)}
    declared = set(_age_gate_sweeps())

    assert gated - declared == set(), (
        f"{sorted(gated - declared)} applies an age gate and has no `age_gate` "
        f"entry in _SWEEP_SPECS, so nothing below asserts which way it fails"
    )
    assert declared - gated == set(), (
        f"{sorted(declared - gated)} carries `age_gate` driving data and no "
        f"longer computes an age, so those cases assert nothing"
    )


def test_each_age_gate_says_what_ITS_sweep_would_have_done():
    """One reason per member, read off the call sites in the module's source.

    The shared reporter takes the consequence as an argument precisely so each
    sweep can name its own — a VPC that may still be in use, a bucket emptied
    before it is deleted, an IAM stack deleted out from under a retained stack.
    Five call sites sharing one sentence would read as a considered decision and
    be the defect this repository's exemption registry exists to prevent, and
    the assertions above pass either way: they match the shared prefix.
    """
    tree = ast.parse(HARNESS.read_text(encoding="utf-8"))
    reasons = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_report_age_unestablishable"
        ):
            assert len(node.args) == 2, ast.unparse(node)
            reason = node.args[1]
            assert isinstance(reason, ast.Constant) and isinstance(reason.value, str), (
                f"the consequence at line {node.lineno} is not a literal string, "
                f"so what it says cannot be read here"
            )
            reasons.append(reason.value)

    assert len(reasons) == len(_age_gate_sweeps()), (
        f"{len(reasons)} age gates report through the shared reporter and "
        f"{len(_age_gate_sweeps())} are registered with `age_gate` driving data"
    )
    assert all(len(r) > 30 for r in reasons), reasons
    assert len(set(reasons)) == len(reasons), (
        f"two age gates give the same consequence, so one sentence is standing "
        f"in for two sweeps whose delete does something different: {reasons}"
    )


@pytest.mark.parametrize("sweep", _age_gate_sweeps())
@pytest.mark.parametrize("value", UNDATEABLE)
def test_an_unestablishable_age_keeps_the_item_and_counts_it(
    cbd, monkeypatch, capsys, sweep, value
):
    """Kept, named, and NOT reported as an incomplete sweep.

    An age that cannot be established is not the same claim as being old enough
    to delete, and these are the paths that issue the delete call against a
    shared CI account. Not a finding, because every item the listing returned
    was considered: ending a night's run red on a skipped item is what would
    make the row that reports a genuinely inert sweep worth less.
    """
    aws = _install(
        cbd, monkeypatch, sweep, with_target=True, contents=_aged(cbd, sweep, value)
    )

    findings = _run(cbd, sweep)

    assert aws.deleted == [], (
        f"{sweep} reaped an item whose age it could not establish, which is the "
        f"gate reading 'cannot tell' as 'old enough to delete'"
    )
    assert findings == [], (
        f"{sweep} turned one skipped item into {findings}; the sweep considered "
        f"everything the listing returned, so it completed"
    )
    out = capsys.readouterr().out
    assert "age not establishable" in out, out
    assert DEAD_RUN in out, "the skipped item is not named anywhere"
    assert cbd.SWEEP_NOT_COMPLETED not in out


@pytest.mark.parametrize("sweep", _age_gate_sweeps())
def test_an_age_past_the_gate_is_still_reaped(cbd, monkeypatch, capsys, sweep):
    """The other direction, or the gate is merely inert.

    A change that keeps everything whose age it is unsure about is only safe
    while a KNOWN age past the gate still reaps, which is the whole purpose of
    these sweeps.
    """
    aws = _install(cbd, monkeypatch, sweep, with_target=True)

    findings = _run(cbd, sweep)

    assert aws.deleted, f"{sweep} reaped nothing although its target is stale"
    assert findings == []
    assert "age not establishable" not in capsys.readouterr().out


@pytest.mark.parametrize("sweep", _age_gate_sweeps())
def test_an_age_inside_the_gate_is_counted_as_young_not_unestablishable(
    cbd, monkeypatch, capsys, sweep
):
    """The two counters must not collapse into one.

    "Younger than the gate" is routine and needs no attention; "age unknown"
    should not happen and names the item. Reporting the first as the second puts
    a `⚠️` on every green deploy, which is how a row stops being read (#1391).
    """
    young = datetime.now(tz=timezone.utc) - timedelta(seconds=YOUNG_AGE_SECONDS)
    aws = _install(
        cbd, monkeypatch, sweep, with_target=True, contents=_aged(cbd, sweep, young)
    )

    findings = _run(cbd, sweep)

    assert aws.deleted == [], f"{sweep} reaped an item younger than its gate"
    assert findings == []
    out = capsys.readouterr().out
    assert "age not establishable" not in out, (
        f"{sweep} reported a young item as one whose age it could not establish, "
        f"collapsing two counters that exist to be read differently"
    )
    young_line = _SWEEP_SPECS[sweep]["age_gate"]["young_line"]
    if young_line:
        assert young_line in out, out


# ---------------------------------------------------------------------------
# The two sweeps whose own handlers must NOT become findings
# ---------------------------------------------------------------------------


def test_an_unreadable_retention_marker_is_not_a_sweep_finding(cbd, monkeypatch):
    """The stack sweep's marker read is protection, not enumeration.

    Its failure leaves no stack unconsidered — the sweep still enumerates and
    reaps — it narrows the protection the age gate is applied with, which is a
    different fact, reported where it happens. Folding it in here would report a
    completed sweep as incomplete, and the clean path has to stay quiet.
    """
    _install(cbd, monkeypatch, "cleanup_stale_idp_stacks", with_target=False)
    monkeypatch.setenv("SOURCE_BUCKET", "a-bucket")
    monkeypatch.setattr(
        cbd,
        "_expired_retention_markers",
        lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("AccessDenied: GetObject")
        ),
    )

    findings = cbd.cleanup_stale_idp_stacks()

    assert findings == []


def test_the_diagnostics_sweep_with_no_bucket_reports_neither_way(cbd, monkeypatch):
    """`None`, not `[]`: it did not run, which is not "ran and found nothing".

    With `SOURCE_BUCKET` unset there is no store to enumerate, so there is no
    claim to make in either direction. Returning `[]` would assert a completed
    sweep over a bucket that was never named — the same conflation one level up.
    """
    monkeypatch.delenv("SOURCE_BUCKET", raising=False)
    assert cbd.cleanup_stale_ci_diagnostics() is None


# ---------------------------------------------------------------------------
# The status table has to surface it, per sweep and per decision
# ---------------------------------------------------------------------------


def _summary(cbd, reported):
    primary = {
        "stack_name": DEAD_RUN,
        "success": True,
        "step_results": {"Step 3: Default config": {"status": "passed", "error": ""}},
        "teardown": {"retained": False, "findings": []},
    }
    return cbd.build_consolidated_summary(DEAD_RUN, primary, [], True, reported)


def _row_for(cbd, text, sweep):
    label = next(s.label for s in cbd.STARTUP_SWEEPS if s.func == sweep)
    rows = [line for line in text.splitlines() if label in line]
    assert len(rows) == 1, (label, rows)
    return rows[0].strip()


@pytest.mark.parametrize("sweep", _sweep_names())
def test_the_table_renders_a_finding_and_applies_this_sweeps_decision(cbd, sweep):
    """The row, and whether the run goes red — two separate claims.

    The verdict alone is not enough in either direction: a row ticked `✅` beside
    a finding still flips OVERALL, so the verdict passes a table that ticks a
    sweep it is failing the run for; and a `❌` on a clean sweep flips nothing, so
    the verdict passes a table that reports a problem on every green deploy.
    """
    entry = next(s for s in cbd.STARTUP_SWEEPS if s.func == sweep)
    text = _summary(cbd, {sweep: [f"{cbd.SWEEP_NOT_COMPLETED} could not enumerate"]})

    assert "could not enumerate" in text, "the finding's text is not in the table"
    row = _row_for(cbd, text, sweep)
    if entry.fails_run:
        assert "OVERALL: FAIL" in text, (
            f"{sweep} is registered fails_run=True and a finding left the table "
            f"reading PASS — the inert sweep is now merely a quieter inert sweep"
        )
        assert row.startswith("❌"), row
    else:
        assert "OVERALL: PASS" in text, (
            f"{sweep} is registered fails_run=False and a finding failed the "
            f"table anyway, which is a decision this registry did not take"
        )
        assert row.startswith("⚠️"), row


@pytest.mark.parametrize("sweep", _sweep_names())
def test_the_table_passes_a_completed_sweep_without_a_warning(cbd, sweep):
    text = _summary(cbd, {s: [] for s in _sweep_names()})

    assert "OVERALL: PASS" in text
    assert cbd.SWEEP_NOT_COMPLETED not in text
    assert _row_for(cbd, text, sweep).startswith("✅")


@pytest.mark.parametrize("sweep", _sweep_names())
def test_the_table_does_not_claim_a_sweep_nobody_reported(cbd, sweep):
    """`None` and an absent key both mean "no result", never "clean"."""
    for reported in ({s: None for s in _sweep_names()}, {}):
        text = _summary(cbd, reported)
        row = _row_for(cbd, text, sweep)
        assert "not reported" in row, row
        assert not row.startswith("✅"), row
        assert "enumerated, swept" not in text


# ---------------------------------------------------------------------------
# main() must consult each outcome, and apply each sweep's own decision
#
# A return value nothing reads is the same inert control one level up, and that
# is the recurring defect class here. Asserted over main()'s syntax tree: the
# question is whether the value is bound and then used to decide the run, which a
# substring search cannot answer.
# ---------------------------------------------------------------------------


def _main_function():
    tree = ast.parse(HARNESS.read_text(encoding="utf-8"))
    main = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    return main


def _bound_name(main, sweep):
    """The local main() binds `sweep()`'s return value to, or None."""
    for node in ast.walk(main):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == sweep
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            return node.targets[0].id
    return None


def _names_in(node):
    return {inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name)}


def _guards_assigning_false(main, name):
    """`if <name>...:` blocks in main() that set `stack_success = False`."""
    return [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.If)
        and name in _names_in(node.test)
        and any(
            isinstance(stmt, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "stack_success"
                for t in stmt.targets
            )
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is False
            for stmt in ast.walk(node)
        )
    ]


@pytest.mark.parametrize("sweep", _sweep_names())
def test_main_binds_every_sweep_result_instead_of_discarding_it(sweep):
    assert _bound_name(_main_function(), sweep), (
        f"main() calls {sweep}() as a bare statement, so its findings go nowhere "
        f"and the sweep's outcome cannot reach the verdict or the table — the "
        f"shape #1434 removes"
    )


@pytest.mark.parametrize("sweep", _sweep_names())
def test_main_applies_each_sweeps_registered_decision(cbd, sweep):
    """Both directions, because only pinning one leaves the other free.

    A fatal sweep whose outcome never flips the verdict is the original defect
    with a return value added. A non-fatal sweep that flips it anyway is a
    decision the registry did not take, and it would end a night's run red over
    month-old diagnostics objects.
    """
    main = _main_function()
    name = _bound_name(main, sweep)
    assert name
    entry = next(s for s in cbd.STARTUP_SWEEPS if s.func == sweep)
    guarded = _guards_assigning_false(main, name)

    if not entry.fails_run:
        assert not guarded, (
            f"{sweep} is registered fails_run=False — see its `why` — but main() "
            f"fails the run on it anyway"
        )
        return

    assert guarded, (
        f"no `if {name}:` in main() sets stack_success = False, so {sweep} "
        f"considering zero targets still leaves the run green"
    )
    # And it must carry a reason, so the 💥 line and the failure email name the
    # sweep rather than just reporting that something went wrong.
    assert any(
        isinstance(stmt, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "failure_reason" for t in stmt.targets
        )
        for node in guarded
        for stmt in ast.walk(node)
    ), f"the `if {name}:` block in main() sets no failure_reason"


@pytest.mark.parametrize("sweep", _sweep_names())
def test_main_routes_every_sweep_result_into_the_status_table(sweep):
    """Keyed by sweep, and the mapping reaches every table call.

    Stronger than "the name appears in some argument": a mapping that drops one
    key renders that sweep as `not reported` on every run, which is the state the
    table is built to make visible rather than one to arrive at silently.
    """
    main = _main_function()
    name = _bound_name(main, sweep)
    assert name

    mappings = {
        target.id
        for node in ast.walk(main)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
        for key, value in zip(node.value.keys, node.value.values)
        if isinstance(key, ast.Constant)
        and key.value == sweep
        and isinstance(value, ast.Name)
        and value.id == name
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert mappings, (
        f"no dict in main() maps {sweep!r} to {name}, so the status table has no "
        f"way to pair this sweep's outcome with its row"
    )

    calls = [
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "build_consolidated_summary"
    ]
    assert calls, "main() builds no consolidated summary"
    uncovered = [
        call
        for call in calls
        if not mappings
        & _names_in(ast.Tuple(elts=[*call.args, *(kw.value for kw in call.keywords)]))
    ]
    assert not uncovered, (
        f"{len(uncovered)} of {len(calls)} build_consolidated_summary() calls in "
        f"main() are not given the sweep mapping, so the in-progress and interim "
        f"tables would claim nothing reported this sweep"
    )


# ---------------------------------------------------------------------------
# The one-line root cause: which sweep it names, and which reason wins
#
# #1433's mutation sample left one survivor here — the precedence of `main()`'s
# one-line `failure_reason` — and recorded it as unpinned, because asserting it
# meant running `main()` end to end. Both halves of it are pinned below instead,
# by naming the two decisions: the sweep a reason is attributed to, and which of
# two reasons is kept. Inverting either mis-sends a reader without changing
# whether the run fails, which is why nothing else notices.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sweep", _sweep_names())
def test_the_one_line_reason_names_the_sweep_it_is_about(cbd, capsys, sweep):
    entry = next(s for s in cbd.STARTUP_SWEEPS if s.func == sweep)
    other_labels = [s.label for s in cbd.STARTUP_SWEEPS if s.func != sweep]

    reason = cbd._report_sweep_not_completed(sweep, ["could not enumerate"])

    assert entry.label in reason, reason
    assert "could not enumerate" in reason, reason
    assert not any(label in reason for label in other_labels), (
        f"the reason for {sweep} names another sweep, so the 💥 line and the "
        f"failure email send a reader after the wrong one: {reason}"
    )
    # Printed unconditionally: `main()` may keep an earlier reason, but a sweep
    # that did not complete has to say so in the log either way.
    assert reason in capsys.readouterr().out


def test_the_first_root_cause_wins_over_a_later_one(cbd):
    """A real deploy failure must not be masked by a Step 0 sweep.

    And the reverse: with nothing else to report, the sweep's reason has to be
    the one that reaches the 💥 line, or the run goes red naming nothing.
    """
    assert cbd._first_failure_reason("publish/build failed: boom", "sweep") == (
        "publish/build failed: boom"
    )
    assert cbd._first_failure_reason("", "sweep did not complete") == (
        "sweep did not complete"
    )


# ---------------------------------------------------------------------------
# The interaction between the stack sweep and the bucket sweep
#
# #1434 asks whether an inert stack sweep makes the bucket sweep's
# protected-prefix set read SMALLER. Measured here, because the direction decides
# whether the bucket sweep's own report can be trusted while the stack sweep is
# broken — and it is the opposite way round. `_live_idp_run_prefixes` protects a
# bucket whose run still has ANY stack, so stacks the stack sweep failed to reap
# keep protecting their buckets: the live set reads LARGER, and the bucket sweep
# reaps FEWER buckets while still truthfully reporting a completed sweep.
#
# The leak is real either way; the attribution is not. It belongs to the stack
# sweep, which is why that one is registered fatal.
# ---------------------------------------------------------------------------


def test_stacks_the_stack_sweep_left_behind_protect_their_buckets(cbd, monkeypatch):
    now = datetime.now(tz=timezone.utc)
    old = now - timedelta(seconds=STALE_AGE_SECONDS)
    bucket = f"{DEAD_RUN}-inputbucket-abc"

    def _reap(leftover_stacks):
        aws = _FakeAws(
            {
                "list_stacks": [
                    {
                        "StackSummaries": [
                            {"StackName": n, "CreationTime": old}
                            for n in leftover_stacks
                        ]
                    }
                ],
                "buckets": [{"Name": bucket, "CreationDate": old}],
            }
        )
        monkeypatch.setattr(cbd.boto3, "client", lambda *a, **k: aws)
        monkeypatch.setattr(cbd.boto3, "resource", lambda *a, **k: _FakeS3Resource())
        return cbd.cleanup_stale_idp_buckets(), aws.deleted

    # The stack sweep did its job: the run has no stacks left.
    findings, deleted = _reap([])
    assert findings == [] and deleted == [bucket]

    # The stack sweep was inert: the run's stacks are still there, so the bucket
    # sweep protects them. Fewer buckets reaped, and still a completed sweep —
    # the protected set reads LARGER, not smaller.
    findings, deleted = _reap([DEAD_RUN, f"{DEAD_RUN}-iam"])
    assert findings == [], (
        "the bucket sweep considered every bucket and skipped one by its own "
        "protection rule, which is a completed sweep — reporting a finding here "
        "would blame it for the stack sweep's failure"
    )
    assert deleted == []


# ---------------------------------------------------------------------------
# The BDA sweep's run liveness: one enumeration, three answers (#1440)
#
# A BDA blueprint or project is reaped only when its name carries a run prefix
# and no stack of that run survives. Whether one does is read from ONE
# enumeration of the account's stacks, `_live_idp_run_prefixes` — the set the
# bucket, role and policy reapers read — made only when a listed name carries a
# run prefix, and the three answers stay apart: the run is live, the run is
# over, or the enumeration did not complete. Only the second deletes.
#
# ⚠️ Every case that keeps a name has a counterpart that reaps one. Protecting
# everything is the careless fix, and it passes every keep-side case while
# making the reaper permanently inert, which is #1434's defect pointed the other
# way.
#
# `list_stacks` honours `StackStatusFilter` here (`_StackListing`) and lists a
# deleted stack as `DELETE_COMPLETE`, the way CloudFormation does, so a filter
# that leaves a surviving status out, or a listing with no filter, fails a case
# instead of passing against a double that ignores both.
# ---------------------------------------------------------------------------


def _bda_contents(*names):
    """Account contents holding one LIVE blueprint per name."""
    return {
        "list_blueprints": [
            {
                "blueprints": [
                    {
                        "blueprintName": name,
                        "blueprintArn": f"arn:fake:blueprint/{name}",
                    }
                    for name in names
                ]
            }
        ],
        "projects": [],
    }


#: Stack names as the harness builds them. The primary run is
#: `generate_stack_name()`. A deployment-variant probe and a transform deploy
#: test append a suffix to a timestamp of their own, so the first three
#: hyphen-separated parts of what such a stack creates name no stack at all. No
#: stack is named the third way today; it is here because nothing in either
#: naming rule keeps a hyphen out of a suffix.
RUN_STACKS = {
    "primary-run": "idp-1015-120000",
    "suffixed": "idp-1015-120507-apigw",
    "hyphenated-suffix": "idp-1015-120507-jobs-api",
}


def _bda_resources_named_for(stack):
    """(blueprints, projects) a stack creates in BDA, named as the code names them.

    A class's blueprint is `<stack>-<class>-<uuid8>` (`BdaBlueprintService` and
    `BlueprintOptimizer`), with the class id passed through `sanitize_class_name`,
    which keeps an underscore. A configuration version's project is
    `<stack>-<version>`, and its bootstrap blueprint `<project>-bootstrap-<uuid8>`.
    The BDA-as-OCR project is `sanitize_ocr_project_name(stack)`. The class ids
    are real ones, one carrying hyphens of its own and one an underscore, and the
    version is the one Step 4 syncs.
    """
    from idp_common.bda.bda_ocr import sanitize_ocr_project_name
    from idp_common.config.class_names import sanitize_class_name

    version_project = f"{stack}-test-bda"
    blueprints = [
        f"{stack}-{sanitize_class_name(class_id)}-abcd1234"
        for class_id in ("Payslip", "US-drivers-licenses", "BANK_CHECK")
    ]
    blueprints.append(f"{version_project}-bootstrap-abcd1234")
    return blueprints, [version_project, sanitize_ocr_project_name(stack)]


def _bda_listing(blueprints, projects):
    """Account contents listing exactly these blueprint and project names."""
    return {
        "list_blueprints": [
            {
                "blueprints": [
                    {
                        "blueprintName": name,
                        "blueprintArn": f"arn:fake:blueprint/{name}",
                    }
                    for name in blueprints
                ]
            }
        ],
        "projects": [
            {"projectName": name, "projectArn": f"arn:fake:project/{name}"}
            for name in projects
        ],
    }


def _arns(blueprints, projects):
    """The ARNs `_bda_listing` gives those names, sorted, as a reap records them."""
    return sorted(
        [f"arn:fake:blueprint/{name}" for name in blueprints]
        + [f"arn:fake:project/{name}" for name in projects]
    )


def _stack_statuses():
    """Every `StackStatus` CloudFormation reports, read off botocore's own model."""
    model = botocore.session.get_session().get_service_model("cloudformation")
    return tuple(model.shape_for("StackStatus").enum)


#: Every status a stack can be listed in but the one a completed delete leaves.
#: Read off the model rather than written out, so a status CloudFormation adds
#: arrives as a case of its own, which fails until the stack enumeration's
#: filter names it.
SURVIVING_STACK_STATUSES = tuple(
    status for status in _stack_statuses() if status != "DELETE_COMPLETE"
)


@pytest.mark.parametrize("shape", list(RUN_STACKS))
def test_a_live_stack_keeps_every_bda_resource_named_for_it(cbd, monkeypatch, shape):
    """Nothing a live stack named after itself is reaped, whatever its name's shape.

    Where the stack name ends inside a BDA name cannot be read off the name: a
    blueprint carries a class and a uuid after it, and a suffixed stack's first
    three parts name no stack. So the sweep asks about the run rather than a
    stack, looking up each name's leading `idp-MMDD-HHMMSS` in one enumeration,
    and asks nothing per name. A question per name multiplied the requests a
    throttle could refuse, and every refused one kept its name and was reported.
    """
    stack = RUN_STACKS[shape]
    blueprints, projects = _bda_resources_named_for(stack)
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_listing(blueprints, projects),
        stacks={stack: "CREATE_COMPLETE"},
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert aws.deleted == [], f"a live stack's BDA resources were reaped: {aws.deleted}"
    assert findings == [], findings
    # One listing per CI region: IAM is global, so a run's stacks are looked for
    # in every region a run can be in (_live_run_regions).
    assert aws.calls.count("list_stacks") == len(cbd._live_run_regions()), aws.calls
    assert aws.described == [], f"asked CloudFormation by name: {aws.described}"


@pytest.mark.parametrize("shape", list(RUN_STACKS))
def test_a_deleted_stacks_bda_resources_are_all_reaped(cbd, monkeypatch, shape):
    """The counterpart: with the run's stack deleted, every one of those names goes.

    The stack is listed the way CloudFormation lists it for 90 days after a
    delete, as `DELETE_COMPLETE`, so this is also the case that the one status a
    completed delete leaves protects nothing. A listing taken with no filter, or
    with that status in it, would keep every name here for those 90 days.
    """
    stack = RUN_STACKS[shape]
    blueprints, projects = _bda_resources_named_for(stack)
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_listing(blueprints, projects),
        stacks={stack: "DELETE_COMPLETE"},
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert findings == [], findings
    assert sorted(aws.deleted) == _arns(blueprints, projects), aws.deleted


def test_a_live_runs_ocr_project_stands_while_a_dead_runs_goes(cbd, monkeypatch):
    """`<stack>_OCR_StdOutput` belongs to `<stack>`'s run, and stands while it is up.

    Every stack in the commercial partition creates that project, named with an
    underscore that a BDA name may carry and a stack name may not, so the name's
    hyphen-separated parts spell no stack. Its leading `idp-MMDD-HHMMSS` is still
    the run's. A concurrent run's sweep used to read the live run's project as
    left over and delete it while that run's stack was up; the dead run's
    project beside it is still reaped.
    """
    from idp_common.bda.bda_ocr import sanitize_ocr_project_name

    live = RUN_STACKS["primary-run"]
    ocr = {run: sanitize_ocr_project_name(run) for run in (live, DEAD_RUN)}
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_listing([], list(ocr.values())),
        stacks={live: "CREATE_IN_PROGRESS", DEAD_RUN: "DELETE_COMPLETE"},
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert findings == [], findings
    assert aws.deleted == [f"arn:fake:project/{ocr[DEAD_RUN]}"], (
        f"expected only the dead run's BDA-as-OCR project to go, got {aws.deleted}"
    )
    assert aws.described == [], aws.described


@pytest.mark.parametrize("status", SURVIVING_STACK_STATUSES)
def test_a_stack_in_any_status_but_delete_complete_keeps_its_runs_bda_resources(
    cbd, monkeypatch, status
):
    """Mid-cleanup, rolling back, under review, failed: the stack is still there.

    The sweep used to keep a run's names only while its stack was in one of
    eight statuses, so a stack finishing an update, rolling back, waiting in
    review, or failed and held for inspection lost its blueprints and projects
    while it stood. Each such stack still owns what it created, and only a
    completed delete says otherwise. The dead run listed beside it is still
    reaped, so no case can pass by the sweep keeping everything.
    """
    stack = RUN_STACKS["suffixed"]
    blueprints, projects = _bda_resources_named_for(stack)
    dead_blueprints, dead_projects = _bda_resources_named_for(DEAD_RUN)
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_listing(blueprints + dead_blueprints, projects + dead_projects),
        stacks={stack: status, DEAD_RUN: "DELETE_COMPLETE"},
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert findings == [], findings
    assert sorted(aws.deleted) == _arns(dead_blueprints, dead_projects), (
        f"a {status} stack's BDA resources were reaped, or the dead run's were "
        f"not: {aws.deleted}"
    )


def test_the_status_cases_reach_every_state_a_standing_stack_was_reaped_in():
    """The derived status set is held to the states it exists to protect.

    `SURVIVING_STACK_STATUSES` comes from botocore's model, so a model that
    failed to load, or changed shape, would quietly parametrise fewer cases.
    These are the states a stack still standing was in while the sweep reaped
    its names, because the eight it protected left them out: both cleanup
    phases, a create or import rollback at any stage, a review, a delete under
    way and every failure. Each has to be a case of its own, and the one status
    that protects nothing has to be a real member of the model rather than a
    name that matches nothing.
    """
    statuses = set(_stack_statuses())
    reaped_while_standing = {
        "CREATE_FAILED",
        "ROLLBACK_IN_PROGRESS",
        "ROLLBACK_FAILED",
        "ROLLBACK_COMPLETE",
        "DELETE_IN_PROGRESS",
        "DELETE_FAILED",
        "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS",
        "UPDATE_FAILED",
        "UPDATE_ROLLBACK_FAILED",
        "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS",
        "REVIEW_IN_PROGRESS",
        "IMPORT_ROLLBACK_IN_PROGRESS",
        "IMPORT_ROLLBACK_FAILED",
        "IMPORT_ROLLBACK_COMPLETE",
    }

    assert "DELETE_COMPLETE" in statuses, sorted(statuses)
    assert "DELETE_COMPLETE" not in SURVIVING_STACK_STATUSES
    missing = sorted(reaped_while_standing - set(SURVIVING_STACK_STATUSES))
    assert not missing, (
        f"{missing} would no longer be a case of their own, so a stack standing "
        f"in one of them could lose its BDA resources again with every case green"
    )
    assert {s for s in statuses if s.endswith("_FAILED")} <= reaped_while_standing


def test_a_stack_retained_for_inspection_keeps_its_runs_bda_resources(cbd, monkeypatch):
    """A failed run's stack, kept standing to be examined, keeps its run's names.

    `retain_failed_stack` holds a failing run's stack for hours, in the state its
    failure left it. A create that failed and rolled back is held as
    `ROLLBACK_COMPLETE`, while the nested stacks the rollback deleted are listed
    as `DELETE_COMPLETE` ahead of it. This sweep reads no retention marker — a
    stack standing is what protects a run — so the one surviving stack has to
    keep the whole run's names, and the run's deleted stacks must not count
    against it. The `-iam` pre-stack that `teardown_primary_stack` keeps beside a
    retained stack would protect the run by itself, so it is left out here and
    the retained stack carries the case alone.
    """
    stack = RUN_STACKS["primary-run"]
    blueprints, projects = _bda_resources_named_for(stack)
    dead_blueprints, dead_projects = _bda_resources_named_for(DEAD_RUN)
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_listing(blueprints + dead_blueprints, projects + dead_projects),
        stacks={
            f"{stack}-PATTERNSTACK-1A2B3C4D5E6F": "DELETE_COMPLETE",
            f"{stack}-DOCUMENTKB-6F5E4D3C2B1A": "DELETE_COMPLETE",
            stack: "ROLLBACK_COMPLETE",
            DEAD_RUN: "DELETE_COMPLETE",
        },
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert findings == [], findings
    assert sorted(aws.deleted) == _arns(dead_blueprints, dead_projects), (
        f"the retained run's BDA resources were reaped, or the dead run's were "
        f"not: {aws.deleted}"
    )


#: Ways the stack enumeration can fail to complete, each of which must leave
#: every name in place. The ones past the throttle class are the point of the
#: list rather than padding: a denial is durable and answers nothing about the
#: account, an unrecognised code has to take the branch a familiar failure
#: takes, the error that means a stack is gone when asked by name means nothing
#: about any stack when the listing raises it, and a connection failure carries
#: no code at all, so the finding has to name the exception's type instead.
STACK_ENUMERATION_ERRORS = (
    pytest.param(_client_error("Throttling", "Rate exceeded"), id="Throttling"),
    pytest.param(
        _client_error("ThrottlingException", "Rate exceeded"),
        id="ThrottlingException",
    ),
    pytest.param(
        _client_error("RequestLimitExceeded", "Request limit exceeded"),
        id="RequestLimitExceeded",
    ),
    pytest.param(
        _client_error(
            "AccessDenied",
            "User is not authorized to perform cloudformation:ListStacks",
        ),
        id="AccessDenied",
    ),
    pytest.param(
        _client_error("SomeCodeNobodyHasSeenYet", "an answer never read against"),
        id="unrecognised-code",
    ),
    pytest.param(_client_error(*STACK_GONE), id="the-by-name-gone-error"),
    pytest.param(ConnectionError("could not reach the endpoint"), id="no-error-code"),
)


@pytest.mark.parametrize("error", STACK_ENUMERATION_ERRORS)
def test_an_unfinished_stack_enumeration_deletes_nothing_and_is_reported_once(
    cbd, monkeypatch, capsys, error
):
    """Could not tell → nothing deleted, and ONE finding however many names.

    Two runs' names, neither run with a stack standing, so with the enumeration
    answered every one of them goes, as the deleted-stack cases above show. What
    keeps them here is the enumeration raising and nothing else. The finding is
    the sweep's rather than any name's, so it does not grow with the account,
    and it carries the cause near its start, where the status table's trim of a
    long detail keeps it.
    """
    blueprints, projects = _bda_resources_named_for(DEAD_RUN)
    other_blueprints, other_projects = _bda_resources_named_for(OTHER_RUN)
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_listing(blueprints + other_blueprints, projects + other_projects),
        stacks={},
        stack_listing_error=(0, error),
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert aws.deleted == [], (
        f"{type(error).__name__} on the stack enumeration deleted {aws.deleted}: "
        f"names of runs this sweep could not establish were over"
    )
    assert len(findings) == 1, findings
    assert findings[0].startswith(cbd.SWEEP_NOT_COMPLETED), findings
    cause = cbd._aws_error_code(error) or type(error).__name__
    assert f"({cause})" in findings[0][:120], (
        f"the cause is not where the status table keeps it: {findings}"
    )
    assert str(error) in findings[0], findings
    assert aws.described == [], aws.described
    assert "No stale BDA resources found" not in capsys.readouterr().out


def test_a_stack_enumeration_that_fails_part_way_deletes_nothing(cbd, monkeypatch):
    """A listing cut short is not a smaller account.

    Two pages answer and the live run's stack is on the third, which does not. A
    set built from the pages that did answer reads that run as over, so the
    failure has to reach the sweep as a failure — and it does only because
    `_live_idp_run_prefixes` raises rather than returning what it had read.
    """
    live = "idp-0303-000002"
    blueprints, projects = _bda_resources_named_for(live)
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_listing(blueprints, projects),
        stacks={
            "idp-0303-000000": "CREATE_COMPLETE",
            "idp-0303-000001": "UPDATE_COMPLETE",
            live: "CREATE_IN_PROGRESS",
        },
        stack_listing_error=(
            2,
            _client_error("ThrottlingException", "Rate exceeded"),
        ),
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert aws.deleted == [], f"reaped from a listing cut short: {aws.deleted}"
    assert len(findings) == 1, findings
    assert findings[0].startswith(cbd.SWEEP_NOT_COMPLETED), findings


def test_the_stacks_are_read_after_the_bda_listings(cbd, monkeypatch):
    """The order is what keeps a run that is starting safe, so it is pinned.

    A BDA resource exists only once the stack that creates it does, so stacks
    read after the BDA listing include the owner of every name it returned,
    while that owner is up. Read first — the order the bucket, role and policy
    reapers use, which their age gates make safe — a run whose stack and first
    resources both appear between the two reads would be listed and not
    protected, and this sweep has no age gate to catch it.
    """
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        stacks={},
    )

    cbd.cleanup_stale_bda_blueprints()

    assert aws.calls == [
        "list_blueprints",
        "list_data_automation_projects",
        *["list_stacks"] * len(cbd._live_run_regions()),
    ], aws.calls


def test_the_liveness_client_rides_out_the_throttle_class_before_it_decides(
    cbd, monkeypatch
):
    """The throttle should not reach the decision at all.

    The adaptive config is the first half of the protection and the
    all-or-nothing enumeration the second: with the retry removed, a single
    `Rate exceeded` costs a night's reaping and a finding. Asserted on the Config
    the sweep's stack enumeration builds its client with, the way the IAM-burst
    equivalent in `test_variant_probes.py` is.
    """
    seen = []
    aws = _FakeAws(_bda_contents(DEAD_RUN))

    def _client(name, *a, **k):
        seen.append((name, k.get("config")))
        return aws

    monkeypatch.setattr(cbd.boto3, "client", _client)
    monkeypatch.setattr(cbd.time, "sleep", lambda *_a, **_k: None)

    cbd.cleanup_stale_bda_blueprints()

    by_service = dict(seen)
    cfg = by_service.get("cloudformation")
    assert cfg is not None, "the liveness client was built without a Config"
    assert cfg.retries["mode"] == "adaptive", cfg.retries
    assert cfg.retries["max_attempts"] >= 5, cfg.retries


def test_an_unfinished_stack_enumeration_is_a_warning_row_and_not_a_red_run(
    cbd, monkeypatch
):
    """The verdict the registry takes for this sweep, applied to its real finding.

    `fails_run=False` was decided for an INERT sweep, and an enumeration that did
    not complete leaves every name in place: the same unmeasured leak, so the
    same verdict. The row still has to render, with the cause in it, because a
    protection that reports nothing is how the reaper's silence was a problem in
    the first place.
    """
    entry = next(
        s for s in cbd.STARTUP_SWEEPS if s.func == "cleanup_stale_bda_blueprints"
    )
    assert entry.fails_run is False

    _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        stacks={},
        stack_listing_error=(
            0,
            _client_error("ThrottlingException", "Rate exceeded"),
        ),
    )
    findings = cbd.cleanup_stale_bda_blueprints()
    text = _summary(cbd, {"cleanup_stale_bda_blueprints": findings})

    assert "OVERALL: PASS" in text
    row = _row_for(cbd, text, "cleanup_stale_bda_blueprints")
    assert row.startswith("⚠️"), row
    assert "ThrottlingException" in row, f"the cause is not in the row: {row}"


@pytest.mark.parametrize(
    "name",
    [
        "idp-weird",
        "idp-weird_x-y",
        "idp-review-Payslip-abcd1234",
        "idp-review_OCR_StdOutput",
        "idp-1015-12000-Payslip-abcd1234",
    ],
)
def test_a_name_with_no_run_prefix_is_never_reaped(cbd, monkeypatch, name):
    """A name that does not open with `idp-MMDD-HHMMSS` is never this sweep's to take.

    The account has no surviving stack at all, the answer that reaps every name
    carrying a run prefix — the dead run's blueprint listed beside it goes — so
    nothing but the missing prefix keeps it. Nothing ties such a name to a run,
    so nothing could establish that its run is over, and the direction matters
    because the name is passed over silently rather than reported: reaped, it
    would be reaped with no check at all. `idp-review` stands for a stack named
    without a timestamp, which names its blueprints and its BDA-as-OCR project
    this way; the second name has three hyphen-separated parts only past an
    underscore, which no stack name can contain; and the last is one digit
    short of a run prefix. Each is listed as a blueprint and as a project, since
    the sweep reaps the two in separate loops.
    """
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_listing([name, DEAD_RUN], [name]),
        stacks={},
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert aws.deleted == [f"arn:fake:blueprint/{DEAD_RUN}"], (
        f"expected only the dead run's blueprint to go, got {aws.deleted}"
    )
    assert findings == [], findings


# ---------------------------------------------------------------------------
# The long-lived stack the stack and bucket sweeps set aside
#
# `idp-ux-nightly` outlives every run, and the stack and bucket sweeps would each
# take it its own way. The stack sweep reaps it and its `-iam` pre-stack once
# they are older than its age gate. The bucket sweep empties and deletes its
# buckets once they are older than its own, with the stack still up, because its
# live-stack protection is read through a run's `idp-MMDD-HHMMSS` prefix and this
# name carries none. Those two are the only sweeps that read the set. The BDA,
# IAM-role and IAM-policy sweeps delete only run-prefixed names and do not read
# it, so for them the long-lived stack rests on the pattern alone: that premise
# is computed below for every member rather than assumed, and measured on each
# of the three with no run live. The BDA sweep reads the account's stacks only
# when a name it listed carries a run prefix, so a stack enumeration that fails
# reports nothing over a listing of the long-lived stack's names alone.
#
# The stack sweep spares the stack and its `-iam` pre-stack by name and nothing
# else. It asks only about top-level stacks, so the long-lived stack's nested
# stacks never reach the question, and any other top-level `idp-ux-nightly-<x>`
# is a stack of its own, reaped once it is old. The bucket sweep spares every
# `idp-ux-nightly-<x>`, which is how CloudFormation names every bucket of it.
# CloudFormation lowercases the stack's name to do that, and shortens a name that
# would run past S3's 63 characters, so the match is known to hold only for a
# member with no capital letter that is short enough for every bucket name to fit
# whole, which is computed below for every member.
#
# Each sweep that reads the set asks it before its age gate. The gates fail
# closed (#1445), so an item a listing did not date is kept and named as one
# whose age could not be established; the long-lived stack's resources are
# counted as not examined instead. The stack sweep also asks it before a run's
# retention marker, and after passing over nested stacks, so it counts
# top-level stacks only. The IAM-role and IAM-policy sweeps pass over every name
# without a run prefix before their age gates, so neither reaches the
# long-lived stack's.
#
# Which sweeps these cases drive is derived from STARTUP_SWEEPS: every member but
# the ones `_sweeps_that_never_read_the_set` names, each with its reason. That
# split, and the list the cases are parametrized over, are held against the
# module's source by `test_every_startup_sweep_reads_the_set_or_says_why_it_does_not`,
# so a sweep added to Step 0 later fails until somebody decides which side it is
# on, and a list narrowed by hand fails there too. The sweeps whose reason is the
# run-prefix rule are read off those reasons by `_run_prefix_only_sweeps`, and
# held to the ones whose own source selects by that prefix.
#
# Every case that drives a sweep lists the long-lived stack's resources BESIDE
# another stack's and asserts that other stack is still reaped, because a sweep
# that stopped reaping altogether would pass a case holding the long-lived stack
# alone. The exceptions are the cases in which nothing can be reaped, and they
# are about what a sweep reports rather than what it deletes: the quiet path,
# which holds the long-lived stack alone, and the BDA cases whose stack
# enumeration is throttled, which delete nothing by design.
# ---------------------------------------------------------------------------

#: Spelled out rather than read off the module, so a change to the module's set
#: fails the pin below instead of re-pointing every case in this section.
LONG_LIVED_STACK = "idp-ux-nightly"

#: An ordinary run listed beside it, whose leftovers every sweep should take.
OLD_RUN_STACK = "idp-1013-131553"

#: Joins a stack's name to its BDA-as-OCR project's, with an underscore rather
#: than a hyphen. Pinned to the function that builds that name in
#: `test_the_ocr_project_named_here_is_the_one_the_stack_creates`.
OCR_PROJECT_SUFFIX = "_OCR_StdOutput"


def _sweeps_that_never_read_the_set():
    """{sweep: why} for each Step 0 sweep that never asks the long-lived stack set.

    One line of reason per sweep, because whether a sweep can take the
    long-lived stack depends on what that sweep deletes. Held against the
    module's source in both directions by
    `test_every_startup_sweep_reads_the_set_or_says_why_it_does_not`. The BDA,
    IAM-role and IAM-policy sweeps' reasons rest on a premise computed rather
    than read: by `test_no_name_of_a_long_lived_stack_can_carry_a_run_prefix`
    for every member, and by
    `test_no_run_prefix_only_sweep_reaps_a_long_lived_name_with_no_run_live`
    against each of those sweeps.
    """
    return {
        "cleanup_stale_apigw_test_vpcs": (
            "takes only idp-*-apigw-vpc stacks, which a manual private-VPC test "
            "leaves behind and nothing creates for the long-lived stack"
        ),
        "cleanup_stale_bda_blueprints": (
            "reaps only names whose leading idp-MMDD-HHMMSS has no surviving "
            "stack, and no name derived from a long-lived stack carries one"
        ),
        "cleanup_stale_idp_roles": (
            "deletes only roles whose leading idp-MMDD-HHMMSS has no surviving "
            "stack, and no role of a long-lived stack, its bastion host's "
            "included, is named that way"
        ),
        "cleanup_stale_idp_policies": (
            "deletes only managed policies whose leading idp-MMDD-HHMMSS has no "
            "surviving stack, which neither a long-lived stack's boundary nor "
            "its pre-stack's PassRole policy carries"
        ),
        "cleanup_stale_ci_diagnostics": (
            "expires evidence bundles in the source bucket after their 30-day "
            "window, which a build writes about a failed run and no stack owns"
        ),
    }


def _long_lived_sweeps():
    """Every STARTUP_SWEEPS member but those named as never reading the set.

    Derived rather than listed, so a sweep added to the registry later is
    driven by every case below, and fails them until the long-lived stack's
    resources are written for it in `_resources_of`, unless it is named in
    `_sweeps_that_never_read_the_set` instead. Held to exactly the sweeps that
    read the set by `test_every_startup_sweep_reads_the_set_or_says_why_it_does_not`,
    so a list written here by hand fails there, naming the sweep it dropped.
    """
    never = _sweeps_that_never_read_the_set()
    return [sweep for sweep in _sweep_names() if sweep not in never]


def _run_prefix_only_sweeps():
    """Each sweep whose reason for leaving the set unread is the run-prefix rule.

    Read off the reasons rather than listed: a sweep named in
    `_sweeps_that_never_read_the_set` for reaping only names whose leading
    `idp-MMDD-HHMMSS` has no surviving stack states a premise, and every such
    sweep is driven by the cases that measure it, so a reason added later is
    measured from the moment it is written, and fails until its listing is
    written in `_listing_of`. Held to the sweeps whose own source selects by
    `_IDP_RUN_PREFIX_RE` by
    `test_every_run_prefix_reason_is_about_a_sweep_that_selects_by_it`.
    """
    never = _sweeps_that_never_read_the_set()
    return [
        sweep for sweep in _sweep_names() if "idp-MMDD-HHMMSS" in never.get(sweep, "")
    ]


#: The template every bucket of an IDP stack is declared in.
IDP_TEMPLATE = HARNESS.parents[2] / "template.yaml"

#: S3's limit on a bucket name. CloudFormation shortens a name it generates that
#: would run past it, so only a name that fits whole is known to open with the
#: whole of the stack's name.
BUCKET_NAME_LIMIT = 63

#: The random part that ends a bucket name CloudFormation generates, at its
#: longest: 13 characters. The buckets benchmarks/results/v0.6.7/clsconf/runmap.json
#: records for a deployed stack carry 12.
GENERATED_SUFFIX = "1a2b3c4d5e6f7"

#: A bucket declaration, quoted or not, wherever a template writes one.
BUCKET_DECLARATION = r"Type: *['\"]?AWS::S3::Bucket\b"


def _bucket_of(stack, logical_id="InputBucket"):
    """What CloudFormation names a bucket of `stack` that its template leaves unnamed.

    `<stack>-<LogicalId>-<suffix>`, lowercased, because a bucket name cannot
    hold a capital letter. No template the IDP stack deploys names a bucket,
    so every bucket of one is named this way wherever the name fits
    BUCKET_NAME_LIMIT whole.
    """
    return f"{stack}-{logical_id}-{GENERATED_SUFFIX}".lower()


def _bucket_declarations():
    """{logical id: the lines under it} for every bucket `template.yaml` declares.

    Read by pattern rather than parsed: each logical id with every line indented
    under it, kept where one of those lines is `Type: AWS::S3::Bucket`. Held to
    the template's own count of bucket declarations by
    `test_every_bucket_of_the_stack_is_declared_unnamed_in_template_yaml`, so
    one written in a shape the pattern misses fails there instead of going
    unread.
    """
    resources = re.findall(
        r"^  (\w+):\n((?:(?:    .*)?\n)*)",
        IDP_TEMPLATE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    return {
        logical_id: lines
        for logical_id, lines in resources
        if re.search(r"^    Type: AWS::S3::Bucket$", lines, re.MULTILINE)
    }


def _nested_templates():
    """The source template of every nested stack `template.yaml` deploys."""
    return [
        IDP_TEMPLATE.parent / directory / "template.yaml"
        for directory in re.findall(
            r"TemplateURL: \./(.+?)/\.aws-sam/packaged\.yaml$",
            IDP_TEMPLATE.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    ]


def _resources_of(sweep, owner):
    """What `sweep` deletes of a stack named `owner`, as `aws.deleted` records it.

    Stacks, buckets and roles are deleted by name; managed policies, BDA
    projects and blueprints by ARN, and `_listing_of` reads their names off the
    ARN's last part. The bucket is named as CloudFormation names one
    (`_bucket_of`). The roles are the `-iam` pre-stack's service role, the two
    `DeletionPolicy: Retain` leaves behind in the knowledge-base stack, and the
    bastion host's role, which `template.yaml` names after the stack with no
    separator. The BDA projects are a versioned one, named `<stack>-<version>`,
    and the BDA-as-OCR one, named `<stack>_OCR_StdOutput`.
    """
    if sweep == "cleanup_stale_idp_stacks":
        return [owner, f"{owner}-iam"]
    if sweep == "cleanup_stale_idp_buckets":
        return [_bucket_of(owner)]
    if sweep == "cleanup_stale_idp_roles":
        return [
            f"{owner}-iam-CFServiceRole",
            f"{owner}-DOCUMENTK-StartIngestionJobFunction-abc",
            f"{owner}-DOCUMENTKB--DataSourceSchedulerRole-xyz",
            f"{owner}BastionHost-Role",
        ]
    if sweep == "cleanup_stale_idp_policies":
        return [
            f"arn:fake:policy/{owner}-PermissionsBoundary",
            f"arn:fake:policy/{owner}-iam-PassRolePolicy",
        ]
    if sweep == "cleanup_stale_bda_blueprints":
        return [
            f"arn:fake:project/{owner}-default",
            f"arn:fake:project/{owner}{OCR_PROJECT_SUFFIX}",
            f"arn:fake:blueprint/{owner}-invoice",
        ]
    raise AssertionError(f"no resources written for {sweep}")


def _listing(sweep, owners):
    """Account contents holding what `sweep` reaps of each stack in `owners`."""
    return _listing_of(
        sweep, [item for owner in owners for item in _resources_of(sweep, owner)]
    )


def _as_listed(sweep, name):
    """What `aws.deleted` would record for `name` listed in `sweep`'s listing.

    A role by its name, a managed policy by its ARN, and a BDA name twice, as a
    blueprint and as a project, since that sweep reaps the two in separate
    loops.
    """
    if sweep == "cleanup_stale_idp_policies":
        return [f"arn:fake:policy/{name}"]
    if sweep == "cleanup_stale_bda_blueprints":
        return [f"arn:fake:blueprint/{name}", f"arn:fake:project/{name}"]
    return [name]


def _listing_of(sweep, found):
    """Account contents listing `found`, each as `aws.deleted` records it.

    The long-lived stack is up, so it and its `-iam` pre-stack are in the
    `list_stacks` the bucket, role and policy sweeps read their live-stack
    protection from. The BDA sweep reads the same enumeration through
    `_StackListing` instead, from the `stacks` each of its cases passes, as does
    any case that passes `stacks`. Every other owner's stacks are gone, except
    from the stack sweep's own listing, where leftover stacks are the targets.
    All of it is older than every age gate.
    """
    old = datetime.now(tz=timezone.utc) - timedelta(seconds=STALE_AGE_SECONDS)
    up = [{"StackName": n} for n in (LONG_LIVED_STACK, f"{LONG_LIVED_STACK}-iam")]

    if sweep == "cleanup_stale_idp_stacks":
        return {
            "list_stacks": [
                {
                    "StackSummaries": [
                        {"StackName": n, "CreationTime": old} for n in found
                    ]
                }
            ]
        }
    if sweep == "cleanup_stale_idp_buckets":
        return {
            "list_stacks": [{"StackSummaries": up}],
            "buckets": [{"Name": n, "CreationDate": old} for n in found],
        }
    if sweep == "cleanup_stale_idp_roles":
        return {
            "list_stacks": [{"StackSummaries": up}],
            "list_roles": [
                {"Roles": [{"RoleName": n, "CreateDate": old} for n in found]}
            ],
        }
    if sweep == "cleanup_stale_idp_policies":
        return {
            "list_stacks": [{"StackSummaries": up}],
            "list_policies": [
                {
                    "Policies": [
                        {
                            "PolicyName": arn.rsplit("/", 1)[-1],
                            "Arn": arn,
                            "CreateDate": old,
                        }
                        for arn in found
                    ]
                }
            ],
        }
    return {
        "list_blueprints": [
            {
                "blueprints": [
                    {"blueprintName": arn.rsplit("/", 1)[-1], "blueprintArn": arn}
                    for arn in found
                    if ":blueprint/" in arn
                ]
            }
        ],
        "projects": [
            {"projectName": arn.rsplit("/", 1)[-1], "projectArn": arn}
            for arn in found
            if ":project/" in arn
        ],
    }


def _install_beside_long_lived(
    cbd, monkeypatch, sweep, owners, contents=None, **kwargs
):
    """`_install` over `_listing`, keeping the S3 resource the bucket sweep uses.

    `_install` records every delete in `aws.deleted`, the policy and role ones
    made through module helpers included. The bucket sweep also empties a
    bucket through `boto3.resource` before deleting it, and emptying is the half
    that loses the data, so the resource is kept for the caller to read.
    `contents` stands in for `_listing`'s, for a case that alters the listing.
    """
    aws = _install(
        cbd,
        monkeypatch,
        sweep,
        with_target=True,
        contents=_listing(sweep, owners) if contents is None else contents,
        **kwargs,
    )
    s3 = _FakeS3Resource()
    monkeypatch.setattr(cbd.boto3, "resource", lambda *a, **k: s3)
    return aws, s3


def _undated_for(contents, field, value, owner):
    """`contents` with `field` set to `value` on every listed item of `owner`'s.

    `_reaged` with a filter, so one listing holds the long-lived stack's items,
    which its age gate cannot date, beside an old run's, which it can. An item
    is `owner`'s when one of its values starts with `owner`; these cases list
    only that stack and an `idp-MMDD-HHMMSS` run, so neither reads as the other.
    """
    if isinstance(contents, dict):
        owned = any(
            isinstance(sub, str) and sub.startswith(owner) for sub in contents.values()
        )
        return {
            key: (
                value
                if owned and key == field
                else _undated_for(sub, field, value, owner)
            )
            for key, sub in contents.items()
        }
    if isinstance(contents, list):
        return [_undated_for(sub, field, value, owner) for sub in contents]
    return contents


def _printed_long_lived_count(out):
    """How many names the sweep said it set aside for a long-lived stack unexamined.

    The one reading of that line for every case here, and it reads only a line
    holding the count alone, so a count folded into another line reads as no
    count at all. The count is of names the sweep asked nothing else about, so
    it says they were not examined rather than that they were saved: the age
    gate would have kept some of them anyway.
    """
    match = re.search(
        r"^ *\((\d+) named for a long-lived stack, not examined\)$", out, re.MULTILINE
    )
    return int(match.group(1)) if match else None


def _printed_bda_skips(out):
    """What the BDA sweep's summary line says it skipped, or None if it said nothing.

    `(names of live runs, live runs, names with no run prefix)`. The line counts
    what it skipped in names, and the runs those names belong to as runs, so
    each number carries its unit.
    """
    match = re.search(
        r"\(skipped (\d+) name\(s\) of (\d+) live run\(s\), "
        r"(\d+) name\(s\) with no run prefix\)",
        out,
    )
    return tuple(int(group) for group in match.groups()) if match else None


def _counted_as_undecided(finding):
    """How many names the BDA sweep's failed stack enumeration left undecided."""
    match = re.search(
        r"none of the (\d+) idp- blueprint\(s\) and project\(s\)", finding
    )
    return int(match.group(1)) if match else None


def test_the_long_lived_stack_set_is_pinned(cbd):
    """Count-pinned: exactly the one stack it was registered with.

    A name added here takes a stack and its `-iam` pre-stack out of the stack
    sweep, which exists because leaked runs exhausted the account's role quota,
    and every bucket named for it out of the bucket sweep. The BDA, IAM-role and
    IAM-policy sweeps reap nothing of that stack's either, since none of its
    names carries a run prefix, so whatever it leaks is left for somebody to
    remove by hand. The expected count is therefore one, the member is the one
    registered in scripts/tests/gate_exemptions.json, and adding a second is an
    edit to this assertion and to that entry rather than a one-word change in
    the harness.
    """
    assert cbd.SWEEP_EXEMPT_STACK_NAMES == frozenset({LONG_LIVED_STACK}), (
        f"SWEEP_EXEMPT_STACK_NAMES is {sorted(cbd.SWEEP_EXEMPT_STACK_NAMES)}, not "
        f"the pinned [{LONG_LIVED_STACK!r}]. Each member holds a stack's IAM roles "
        f"for as long as it stands and nothing reaps what it leaks: change this "
        f"pin and its entry in scripts/tests/gate_exemptions.json together, with "
        f"the reason the new stack has to outlive every run"
    )


#: The registry and the entry in it that this module is the count-pinned evidence
#: for.
REGISTRY = Path(__file__).resolve().parents[2] / "tests" / "gate_exemptions.json"
REGISTRY_KEY = "scripts/sdlc/codebuild_deployment.py::SWEEP_EXEMPT_STACK_NAMES"

#: How the entry's `turnsOff` spells a count of sweeps.
COUNT_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven")


def test_the_registry_entry_states_the_sweeps_that_read_the_set():
    """Count-pinned: the entry names, and counts, exactly the sweeps driven below.

    `turnsOff` says how many Step 0 sweeps the set turns reaping off for and
    names each one, and both are facts about the harness that go stale the
    moment a sweep starts or stops reading the set. So the count it states and
    the sweeps it names are held to `_long_lived_sweeps()`, which the closure
    test below holds to the module's source.
    """
    entry = json.loads(REGISTRY.read_text(encoding="utf-8"))["exemptions"][REGISTRY_KEY]
    turns_off = entry["turnsOff"]
    stated = re.match(r"reaping by (\w+) of the integration harness's ", turns_off)
    named = set(re.findall(r"\bcleanup_stale_\w+", turns_off))
    driven = _long_lived_sweeps()

    assert stated, f"{REGISTRY_KEY}'s turnsOff states no count: {turns_off}"
    assert stated.group(1) == COUNT_WORDS[len(driven)], (
        f"{REGISTRY_KEY} says the set turns reaping off for {stated.group(1)} "
        f"sweeps, and {len(driven)} read it: {driven}"
    )
    assert named == set(driven), (
        f"{REGISTRY_KEY}'s turnsOff names {sorted(named)}, and the sweeps that "
        f"read the set are {sorted(driven)}"
    )


def test_every_startup_sweep_reads_the_set_or_says_why_it_does_not():
    """Universe closure over STARTUP_SWEEPS, held against the module's source.

    The cases in this section drive every registered sweep but the ones
    `_sweeps_that_never_read_the_set` names, and that split is closed by
    construction until each side is read off the harness: a sweep driven here
    must reach a function that reads SWEEP_EXEMPT_STACK_NAMES, and a sweep named
    as never reading it must not. So a sweep added to Step 0 later fails until
    somebody decides — here if it does not consult the set, and in every case
    below either way, for want of the long-lived stack's resources in
    `_resources_of` — whether it consults the set and has those written for it,
    or is named with the reason it has nothing of that stack's to take. A sweep
    that stops consulting the set fails here the same way.

    The list those cases are parametrized over, `_long_lived_sweeps()`, is held
    to the same answer, because the split above is only as good as the list
    that drives them: one written by hand that drops a sweep reading the set
    leaves that sweep with no long-lived case at all, and every other check
    here still passes.
    """
    functions = _top_level_functions(ast.parse(HARNESS.read_text(encoding="utf-8")))

    def _reads_the_set(name, seen):
        if name in seen or name not in functions:
            return False
        seen.add(name)
        body = functions[name]
        return any(
            isinstance(node, ast.Name) and node.id == "SWEEP_EXEMPT_STACK_NAMES"
            for node in ast.walk(body)
        ) or any(
            _reads_the_set(callee, seen)
            for callee in sorted(_called_function_names(body))
        )

    registered = set(_sweep_names())
    never = _sweeps_that_never_read_the_set()
    reading = {sweep for sweep in registered if _reads_the_set(sweep, set())}

    assert set(never) <= registered, (
        f"_sweeps_that_never_read_the_set names {sorted(set(never) - registered)}, "
        f"which STARTUP_SWEEPS does not list, so its reason is about nothing"
    )
    assert not reading & set(never), (
        f"{sorted(reading & set(never))} reads SWEEP_EXEMPT_STACK_NAMES and is "
        f"also named as never reading it, so no case here drives a sweep that "
        f"does consult the set: drop it from _sweeps_that_never_read_the_set"
    )
    assert registered - reading - set(never) == set(), (
        f"{sorted(registered - reading - set(never))} never reads "
        f"SWEEP_EXEMPT_STACK_NAMES and is not named in "
        f"_sweeps_that_never_read_the_set, so whether it can take the long-lived "
        f"stack is decided by nobody. Consult the set and write that stack's "
        f"resources for it into _resources_of, or name it there with the reason "
        f"it has nothing of that stack's to take"
    )
    for sweep, why in never.items():
        assert why.strip() and "\n" not in why, (sweep, why)
    assert len(set(never.values())) == len(never), (
        "two sweeps give the same reason for leaving the set unread, so one "
        "sentence is standing in for two members"
    )
    driven = set(_long_lived_sweeps())
    assert driven == reading, (
        f"the long-lived cases below drive {sorted(driven)}, not the sweeps that "
        f"read SWEEP_EXEMPT_STACK_NAMES, {sorted(reading)}; they differ in "
        f"{sorted(driven ^ reading)}. One missing from the first consults the set "
        f"with no case holding it to that, and one extra is driven against a set "
        f"it never reads: _long_lived_sweeps derives its list from STARTUP_SWEEPS, "
        f"and is not to be written by hand"
    )


@pytest.mark.parametrize(
    ("name", "named_for_it"),
    [
        (f"{LONG_LIVED_STACK}-iam", True),
        (_bucket_of(LONG_LIVED_STACK), True),
        (f"{LONG_LIVED_STACK}-patternstack-1a2b3c4d5e6f-workingbucket-abc", True),
        (LONG_LIVED_STACK, False),
        (f"{LONG_LIVED_STACK}{OCR_PROJECT_SUFFIX}", False),
        (f"{LONG_LIVED_STACK}BastionHost-Role", False),
        ("idp-ux-nightlyx", False),
        ("idp-ux-nightly2", False),
        ("idp-ux-nightly2-iam", False),
        (f"idp-ux-nightly2{OCR_PROJECT_SUFFIX}", False),
        ("idp-ux-nightl", False),
        ("xidp-ux-nightly", False),
        (OLD_RUN_STACK, False),
    ],
)
def test_only_names_under_the_stack_match(cbd, name, named_for_it):
    """The bucket sweep's match: `<stack>-…`.

    The shape of every bucket name of the stack, and the match takes a name
    under it however many parts follow the hyphen. No bucket is named the
    stack's bare name, so that does not match. The match stops at the hyphen,
    so two other names of the stack that a Step 0 sweep lists do not match
    either: its BDA-as-OCR project, joined by an underscore, and its bastion
    host's role, joined by nothing at all. The bucket sweep lists neither, and
    the BDA and IAM-role sweeps keep them by their run-prefix rule without
    reading the set. A bare prefix match would take `idp-ux-nightly2`'s
    buckets, another stack's, out of that sweep along with the ones that are
    meant to stay.
    """
    assert cbd._named_for_exempt_stack(name) is named_for_it


def test_every_bucket_of_the_stack_is_declared_unnamed_in_template_yaml():
    """Where the names the premise below is computed over come from.

    Every bucket declaration in `template.yaml` is read, none of them carries a
    `BucketName`, and no nested stack it deploys declares a bucket, so
    CloudFormation names each bucket the stack has after the stack itself.
    """
    declared = re.findall(BUCKET_DECLARATION, IDP_TEMPLATE.read_text(encoding="utf-8"))
    declarations = _bucket_declarations()
    nested = _nested_templates()

    assert declarations, "no bucket declaration was read off template.yaml"
    assert len(declarations) == len(declared), (
        f"template.yaml declares {len(declared)} buckets and "
        f"{sorted(declarations)} were read: one is written in a shape "
        f"_bucket_declarations does not read"
    )
    named = [
        logical_id
        for logical_id, lines in declarations.items()
        if re.search(r"^      BucketName:", lines, re.MULTILINE)
    ]
    assert named == [], (
        f"{named} name their bucket, so CloudFormation does not name it after the "
        f"stack, and nothing here says whether the bucket sweep's match reaches it"
    )
    assert nested, "template.yaml names no nested stack's template"
    declaring = [
        str(path.relative_to(IDP_TEMPLATE.parent))
        for path in nested
        if re.search(BUCKET_DECLARATION, path.read_text(encoding="utf-8"))
    ]
    assert declaring == [], (
        f"{declaring} declare a bucket, which CloudFormation names after a stack "
        f"of its own, itself named <stack>-<LogicalId>-<suffix>, shortening the "
        f"name where it would run past S3's limit, so nothing here says whether "
        f"the bucket sweep's match reaches it"
    )


def _members_the_bucket_match_misses(cbd):
    """Each member with a bucket the bucket sweep's match is not shown to reach.

    A name past BUCKET_NAME_LIMIT is one CloudFormation shortens, so it counts
    as missed whatever the match makes of it.
    """
    return [
        stack
        for stack in sorted(cbd.SWEEP_EXEMPT_STACK_NAMES)
        if not all(
            len(name) <= BUCKET_NAME_LIMIT and cbd._named_for_exempt_stack(name)
            for name in (_bucket_of(stack, lid) for lid in _bucket_declarations())
        )
    ]


def test_the_bucket_match_reaches_every_members_buckets(cbd, monkeypatch):
    """What the bucket sweep's exception rests on, computed for every member.

    The names are the ones CloudFormation gives every bucket `template.yaml`
    declares. It lowercases the stack's name in them, and the match compares
    the member as written, so a member with a capital letter leaves them
    unmatched. It shortens a name that would run past S3's limit, so a member
    too long for every one of them to fit whole leaves the shortened ones
    unmatched as far as anything here can show. Either way those names carry no
    run prefix for the live-stack protection to read, so the bucket sweep would
    empty and delete them once they passed its age gate, with the stack still
    up. A lowercase member and a match that folds case answer the first
    equally, since no bucket name holds a capital letter, and only a shorter
    member answers the second, so the member that shows this check can fail is
    a long one. The names are held to S3's rule too, so the model they are
    computed from cannot drift to names no bucket can have.
    """
    names = [
        _bucket_of(stack, logical_id)
        for stack in sorted(cbd.SWEEP_EXEMPT_STACK_NAMES)
        for logical_id in _bucket_declarations()
    ]
    missed = _members_the_bucket_match_misses(cbd)

    assert missed == [], (
        f"{missed}: the bucket sweep's match is not shown to reach every name "
        f"CloudFormation gives that stack's buckets, so it could reap one with "
        f"the stack up. Name the member in lowercase, short enough for "
        f"<member>-<LogicalId>-{GENERATED_SUFFIX} to fit {BUCKET_NAME_LIMIT} "
        f"characters for every bucket template.yaml declares"
    )
    not_bucket_names = [
        name for name in names if not re.fullmatch(r"[a-z0-9][a-z0-9-]*[a-z0-9]", name)
    ]
    assert names and not_bucket_names == [], (
        f"{not_bucket_names} hold a character no bucket name can, so _bucket_of no "
        f"longer names a bucket as S3 requires and the check above is about names "
        f"CloudFormation never gives"
    )
    too_long = f"{LONG_LIVED_STACK}-review-builds"
    monkeypatch.setattr(cbd, "SWEEP_EXEMPT_STACK_NAMES", frozenset({too_long}))
    assert _members_the_bucket_match_misses(cbd) == [too_long]


@pytest.mark.parametrize(
    ("name", "spared"),
    [
        (LONG_LIVED_STACK, True),
        (f"{LONG_LIVED_STACK}-iam", True),
        (f"{LONG_LIVED_STACK}-test", False),
        (f"{LONG_LIVED_STACK}-test-iam", False),
        (f"{LONG_LIVED_STACK}-iam-test", False),
        (f"{LONG_LIVED_STACK}-feature-confbench-testset", False),
        (f"{LONG_LIVED_STACK}-VPCEndpoints", False),
        ("idp-ux-nightly2", False),
        ("idp-ux-nightly2-iam", False),
    ],
)
def test_the_stack_sweep_spares_the_stack_and_its_pre_stack_and_nothing_else(
    cbd, name, spared
):
    """Two names, where the bucket sweep spares every bucket under the stack.

    The stack sweep asks only about top-level stacks, so the long-lived stack's
    nested stacks never reach this, and any other top-level stack named
    `idp-ux-nightly-<x>` is a stack of its own: a throwaway named after the
    long-lived one, a feature extension installed into it under the default
    stack name `<host>-feature-<id>`, or the endpoints stack
    `scripts/deploy-vpc-endpoints.py` deploys for it under the default
    `<host>-VPCEndpoints`. Spared by a prefix match, each would stand until
    somebody removed it by hand, and a throwaway would hold its IAM roles all
    that time.
    """
    assert cbd._is_exempt_stack_or_pre_stack(name) is spared


def test_the_ocr_project_named_here_is_the_one_the_stack_creates():
    """The underscore-joined name above is the real one, not a guess at it.

    `Custom::BDAOCRProject` names its project from the main stack's name with
    the function `src/lambda/bda_ocr_project` inlines and is pinned against. Were
    that name to change, the BDA cases in this section would go on listing a
    name the stack no longer creates.
    """
    from idp_common.bda.bda_ocr import sanitize_ocr_project_name

    assert sanitize_ocr_project_name(LONG_LIVED_STACK) == (
        f"{LONG_LIVED_STACK}{OCR_PROJECT_SUFFIX}"
    ), (
        f"{LONG_LIVED_STACK}'s BDA-as-OCR project is now named "
        f"{sanitize_ocr_project_name(LONG_LIVED_STACK)!r}: update OCR_PROJECT_SUFFIX"
    )


def test_the_bastion_role_named_here_is_the_one_the_template_names():
    """The role joined to the stack's name by nothing is the real one, too.

    `template.yaml` names the bastion host's role after the stack with no
    separator. It names the host's instance profile and launch template that
    way too, and no Step 0 sweep deletes either, so of the stack's names joined
    to it by nothing, the role is the one a sweep lists. Were that name to
    change, the role cases in this section would go on listing a name the stack
    no longer creates.
    """
    template = (HARNESS.parents[2] / "template.yaml").read_text(encoding="utf-8")

    assert "RoleName: !Sub '${AWS::StackName}BastionHost-Role'" in template, (
        "template.yaml no longer names the bastion host's role "
        "<stack>BastionHost-Role: update _resources_of"
    )


@pytest.mark.parametrize("sweep", _long_lived_sweeps())
def test_the_long_lived_stack_stands_while_an_old_run_beside_it_is_reaped(
    cbd, monkeypatch, capsys, sweep
):
    """Per sweep: nothing of the long-lived stack goes, the old run all goes.

    Each of these sweeps would take the long-lived stack's resources without
    the set, so the deletes are what pin its check, and the printed count says
    the sweep set those resources aside rather than leaving them for some other
    reason.
    """
    aws, s3 = _install_beside_long_lived(
        cbd, monkeypatch, sweep, [LONG_LIVED_STACK, OLD_RUN_STACK]
    )

    findings = _run(cbd, sweep)

    assert findings == [], findings
    assert sorted(aws.deleted) == sorted(_resources_of(sweep, OLD_RUN_STACK)), (
        f"{sweep} deleted {aws.deleted}. Expected exactly the old run's "
        f"{_resources_of(sweep, OLD_RUN_STACK)}: anything of {LONG_LIVED_STACK} "
        f"is the long-lived stack lost, and anything short of the old run is a "
        f"sweep that stopped reaping"
    )
    if sweep == "cleanup_stale_idp_buckets":
        assert s3.emptied == _resources_of(sweep, OLD_RUN_STACK), (
            f"the bucket sweep emptied {s3.emptied}; emptying loses the long-lived "
            f"stack's data even where the delete after it then fails"
        )
    printed = _printed_long_lived_count(capsys.readouterr().out)
    assert printed == len(_resources_of(sweep, LONG_LIVED_STACK)), (
        f"{sweep} printed {printed} as the count it set aside, not examined, for "
        f"a long-lived stack"
    )


@pytest.mark.parametrize("lookalike", ["idp-ux-nightlyx", "idp-ux-nightly2"])
@pytest.mark.parametrize("sweep", _long_lived_sweeps())
def test_a_lookalike_of_the_long_lived_stack_gets_no_such_protection(
    cbd, monkeypatch, capsys, sweep, lookalike
):
    """A different stack, so it is reaped like any other leftover.

    Nor is it counted as the long-lived stack's.
    """
    aws, _ = _install_beside_long_lived(
        cbd, monkeypatch, sweep, [LONG_LIVED_STACK, lookalike]
    )

    findings = _run(cbd, sweep)

    assert findings == [], findings
    reaped = _resources_of(sweep, lookalike)
    assert sorted(aws.deleted) == sorted(reaped), (
        f"{sweep} deleted {aws.deleted} with {lookalike} beside {LONG_LIVED_STACK}; "
        f"expected {reaped}"
    )
    assert _printed_long_lived_count(capsys.readouterr().out) == len(
        _resources_of(sweep, LONG_LIVED_STACK)
    ), f"{sweep} counted {lookalike}'s resources as the long-lived stack's"


@pytest.mark.parametrize("sweep", _long_lived_sweeps())
def test_a_listing_of_only_the_long_lived_stack_is_still_the_quiet_path(
    cbd, monkeypatch, capsys, sweep
):
    """Nothing to reap, so the pinned quiet line is printed whole and unchanged.

    The count goes on a line of its own above it.
    """
    aws, s3 = _install_beside_long_lived(cbd, monkeypatch, sweep, [LONG_LIVED_STACK])

    findings = _run(cbd, sweep)

    assert findings == [], findings
    assert aws.deleted == [] and s3.emptied == []
    out = capsys.readouterr().out
    lines = out.splitlines()
    quiet_line = _SWEEP_SPECS[sweep]["quiet_line"]
    assert quiet_line in lines, out
    assert cbd.SWEEP_NOT_COMPLETED not in out
    above = "\n".join(lines[: lines.index(quiet_line)])
    assert _printed_long_lived_count(above) == len(
        _resources_of(sweep, LONG_LIVED_STACK)
    ), out


#: Runs listed beside the long-lived stack so a sweep has something for every
#: other line it prints about what it kept: one inside every age gate, one kept
#: for a live stack or a retention marker, one whose marker could not be read,
#: and one the listing did not date.
YOUNG_RUN = "idp-1014-090000"
KEPT_RUN = "idp-1014-100000"
UNREAD_MARKER_RUN = "idp-1014-110000"
UNDATED_RUN = "idp-1014-120000"


def _beside_every_other_count(cbd, monkeypatch, sweep):
    """`sweep` over the long-lived stack, an old run, and a run for each other count.

    Returns the fake account, the S3 resource the bucket sweep empties through,
    and a fragment of each other line the listing makes `sweep` print about
    what it kept, so a case can tell that every one of them printed.
    """
    now = datetime.now(tz=timezone.utc)
    young = now - timedelta(seconds=YOUNG_AGE_SECONDS)
    old = now - timedelta(seconds=STALE_AGE_SECONDS)
    owners = [LONG_LIVED_STACK, OLD_RUN_STACK]
    contents = _listing(sweep, owners)
    if sweep == "cleanup_stale_idp_stacks":
        contents["list_stacks"][0]["StackSummaries"] += [
            {"StackName": YOUNG_RUN, "CreationTime": young},
            {"StackName": KEPT_RUN, "CreationTime": old},
            {"StackName": UNREAD_MARKER_RUN, "CreationTime": old},
            {"StackName": UNDATED_RUN, "CreationTime": None},
        ]
        fragments = (
            "retained idp- stack(s) skipped",
            "young idp- stack(s) skipped",
            "retention marker could not be read",
            "age not establishable",
        )
    elif sweep == "cleanup_stale_idp_buckets":
        contents["list_stacks"][0]["StackSummaries"].append({"StackName": KEPT_RUN})
        contents["buckets"] += [
            {"Name": _bucket_of(YOUNG_RUN), "CreationDate": young},
            {"Name": _bucket_of(KEPT_RUN), "CreationDate": old},
            {"Name": _bucket_of(UNDATED_RUN), "CreationDate": None},
        ]
        fragments = (
            "1 protected by a live stack",
            "1 younger than the age gate",
            "age not establishable",
        )
    else:
        raise AssertionError(f"no other counts written for {sweep}")
    aws, s3 = _install_beside_long_lived(
        cbd, monkeypatch, sweep, owners, contents=contents
    )
    if sweep == "cleanup_stale_idp_stacks":
        monkeypatch.setenv("SOURCE_BUCKET", "a-bucket")
        markers = (
            {KEPT_RUN: (now + timedelta(days=1)).isoformat()},
            [],
            {UNREAD_MARKER_RUN: "AccessDenied"},
        )
        monkeypatch.setattr(cbd, "_expired_retention_markers", lambda *_a: markers)
    return aws, s3, fragments


@pytest.mark.parametrize("sweep", _long_lived_sweeps())
def test_the_long_lived_count_keeps_its_own_line_beside_every_other_count(
    cbd, monkeypatch, capsys, sweep
):
    """The count keeps a line of its own while every other count line prints.

    Those lines print only when an item they count is listed, and the cases
    above list none, so a sweep that folded the long-lived count into one of
    them whenever it printed would pass all of those. Here a run inside the age
    gate, a run kept for a live stack or a retention marker, and a run the
    listing did not date are listed beside the long-lived stack and the old run,
    and for the stack sweep a run whose marker could not be read as well. Each
    of those lines prints, the long-lived count is still read from a line that
    holds it alone, and only the old run goes.
    """
    aws, s3, fragments = _beside_every_other_count(cbd, monkeypatch, sweep)

    findings = _run(cbd, sweep)

    assert findings == [], findings
    assert sorted(aws.deleted) == sorted(_resources_of(sweep, OLD_RUN_STACK)), (
        f"{sweep} deleted {aws.deleted}; expected exactly the old run's "
        f"{_resources_of(sweep, OLD_RUN_STACK)}"
    )
    if sweep == "cleanup_stale_idp_buckets":
        assert s3.emptied == _resources_of(sweep, OLD_RUN_STACK), s3.emptied
    out = capsys.readouterr().out
    assert [fragment for fragment in fragments if fragment not in out] == [], out
    assert _printed_long_lived_count(out) == len(
        _resources_of(sweep, LONG_LIVED_STACK)
    ), out


@pytest.mark.parametrize(
    "statuses",
    [
        pytest.param({LONG_LIVED_STACK: "UPDATE_COMPLETE"}, id="up"),
        pytest.param(
            {LONG_LIVED_STACK: "UPDATE_ROLLBACK_FAILED"},
            id="update-rollback-failed",
        ),
        pytest.param(
            {LONG_LIVED_STACK: "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS"},
            id="update-cleanup-in-progress",
        ),
        pytest.param(
            {LONG_LIVED_STACK: "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS"},
            id="rollback-cleanup-in-progress",
        ),
        pytest.param({}, id="not-listed"),
    ],
)
def test_the_bda_sweep_never_asks_after_the_long_lived_stack(
    cbd, monkeypatch, capsys, statuses
):
    """Its status cannot make its BDA resources leftovers, so it is not read.

    The stack is listed up, in both cleanup phases a stack updated in place
    passes through, and stopped in a failed rollback; in the last case it is not
    listed at all. The five take one path through the sweep: the stack's names
    carry no run prefix, so none of them is a candidate whatever the
    enumeration says of the stack, and its status reaches no decision. They are
    kept as the states a run can find the stack in, so that a sweep which came
    to read that status, or to need the stack listed, fails in whichever one it
    misreads. The sweep does not read the long-lived set either: with the old
    run's stack deleted, no run is live in any of these cases. Nothing is asked
    about the stack by name, since every run's liveness is read from one
    enumeration of the account. The old run in the same listing is listed as
    deleted, read from that enumeration, and still reaped, and the stack's names
    are counted among the ones with no run prefix. What a failed enumeration
    reports is pinned by
    `test_a_listing_of_only_long_lived_names_reads_no_stacks` and
    `test_a_failed_stack_enumeration_counts_only_the_names_it_left_undecided`.
    """
    aws, _ = _install_beside_long_lived(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        [LONG_LIVED_STACK, OLD_RUN_STACK],
        stacks={**statuses, OLD_RUN_STACK: "DELETE_COMPLETE"},
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert findings == [], findings
    assert aws.described == [], f"the sweep asked CloudFormation about {aws.described}"
    # One listing per CI region: IAM is global, so a run's stacks are looked for
    # in every region a run can be in (_live_run_regions).
    assert aws.calls.count("list_stacks") == len(cbd._live_run_regions()), aws.calls
    assert sorted(aws.deleted) == sorted(
        _resources_of("cleanup_stale_bda_blueprints", OLD_RUN_STACK)
    ), aws.deleted
    assert _printed_bda_skips(capsys.readouterr().out) == (
        0,
        0,
        len(_resources_of("cleanup_stale_bda_blueprints", LONG_LIVED_STACK)),
    )


def _names_of_a_long_lived_stack(stack):
    """Every shape a name derived from `stack` takes in the run-prefix-only listings.

    What the stack creates in BDA, named as the code names it
    (`_bda_resources_named_for`): its class blueprints, a configuration
    version's project and that project's bootstrap blueprint, and the
    BDA-as-OCR project `<stack>_OCR_StdOutput`. Its IAM roles and managed
    policies as `_resources_of` writes them, the bastion host's role, joined to
    the stack's name with no separator, among them. Then the stack itself, its
    `-iam` pre-stack, and the stack joined by nothing, `-` or `_` to two tails:
    the rest of `DEAD_RUN` after as many characters as `<stack><sep>` already
    has, and the whole of `DEAD_RUN`. A run prefix is a fixed sequence of
    characters, each a literal or a digit, read from the start of the name, so
    a name that opens with `<stack><sep>` can carry one only if the first tail
    completes one; joined by nothing, that covers every name that opens with the
    stack's.
    """
    blueprints, projects = _bda_resources_named_for(stack)
    iam = [
        listed.rsplit("/", 1)[-1]
        for sweep in ("cleanup_stale_idp_roles", "cleanup_stale_idp_policies")
        for listed in _resources_of(sweep, stack)
    ]
    joined = [
        f"{stack}{sep}{tail}"
        for sep in ("", "-", "_")
        for tail in (DEAD_RUN[len(stack) + len(sep) :], DEAD_RUN)
    ]
    return [stack, f"{stack}-iam", *blueprints, *projects, *iam, *joined]


def _long_lived_names(cbd):
    """`_names_of_a_long_lived_stack` for every member of the module's set."""
    return [
        name
        for stack in sorted(cbd.SWEEP_EXEMPT_STACK_NAMES)
        for name in _names_of_a_long_lived_stack(stack)
    ]


def test_no_name_of_a_long_lived_stack_can_carry_a_run_prefix(cbd):
    """What the run-prefix-only sweeps' reasons for leaving the set unread rest on.

    The BDA, IAM-role and IAM-policy sweeps each reap a name once its leading
    `idp-MMDD-HHMMSS` has no surviving stack, and none reads the long-lived
    set, so a long-lived stack's blueprints, projects, roles and policies stand
    only while no name that opens with the stack's carries a run prefix.
    Computed for every member of the module's set rather than for the pinned
    one, so a member named in a way that breaks it fails here instead of being
    reaped there. `DEAD_RUN` has to be a whole run prefix for the completing
    tail to mean anything, and the same names built from a run's own stack, or
    from a stack name that opens like one, are caught, so this is not a check
    nothing could fail.
    """
    pattern = cbd._IDP_RUN_PREFIX_RE
    assert pattern.fullmatch(DEAD_RUN), DEAD_RUN

    carrying = [name for name in _long_lived_names(cbd) if pattern.match(name)]

    assert carrying == [], (
        f"{carrying} carry a run prefix, so each of {_run_prefix_only_sweeps()} "
        f"reaps them once no stack of that run survives, with the long-lived "
        f"stack still up: read the set in those sweeps again, or rename the member"
    )
    for opens_like_a_run in (DEAD_RUN, "idp-0101"):
        assert any(
            pattern.match(name)
            for name in _names_of_a_long_lived_stack(opens_like_a_run)
        ), opens_like_a_run


@pytest.mark.parametrize("sweep", _run_prefix_only_sweeps())
def test_no_run_prefix_only_sweep_reaps_a_long_lived_name_with_no_run_live(
    cbd, monkeypatch, capsys, sweep
):
    """The other half of that premise, measured on each sweep it is the reason for.

    The account holds no stack at all, so the enumeration answers with nothing
    and no run is live: the answer that reaps every name carrying a run prefix,
    and the dead run's names listed beside them, older than every age gate, all
    go. Every name `_names_of_a_long_lived_stack` builds for every member is
    listed too, equally old, and none of them goes. The BDA sweep counts each
    among the names with no run prefix, listed as a blueprint and as a
    project, so none was dropped from the listing before that sweep saw it.
    """
    names = [
        listed for name in _long_lived_names(cbd) for listed in _as_listed(sweep, name)
    ]
    dead = _resources_of(sweep, DEAD_RUN)
    aws = _install(
        cbd,
        monkeypatch,
        sweep,
        with_target=True,
        contents=_listing_of(sweep, [*names, *dead]),
        stacks={},
    )

    findings = _run(cbd, sweep)

    assert findings == [], findings
    assert sorted(aws.deleted) == sorted(dead), (
        f"{sweep} deleted {aws.deleted}; expected exactly the dead run's {dead}"
    )
    assert aws.described == [], aws.described
    if sweep == "cleanup_stale_bda_blueprints":
        assert _printed_bda_skips(capsys.readouterr().out) == (0, 0, len(names))


@pytest.mark.parametrize("value", UNDATEABLE)
@pytest.mark.parametrize(
    "sweep", [s for s in _run_prefix_only_sweeps() if _SWEEP_SPECS[s]["age_gate"]]
)
def test_a_long_lived_name_never_reaches_a_run_prefix_only_sweeps_age_gate(
    cbd, monkeypatch, capsys, sweep, value
):
    """Passed over with every name that has no run prefix, before the age gate.

    The age gate fails closed (#1445) and names an item it cannot date, so a
    sweep that asked it before the run prefix would report the long-lived
    stack's undated names on a `⚠️ … age not establishable` line on every run
    whose listing left them undated, though it never deletes them. The dead
    run's names beside them keep their dates and are still reaped.
    """
    names = [
        listed for name in _long_lived_names(cbd) for listed in _as_listed(sweep, name)
    ]
    dead = _resources_of(sweep, DEAD_RUN)
    field = _SWEEP_SPECS[sweep]["age_gate"]["field"]
    contents = _undated_for(
        _listing_of(sweep, [*names, *dead]), field, value, LONG_LIVED_STACK
    )
    aws = _install(
        cbd, monkeypatch, sweep, with_target=True, contents=contents, stacks={}
    )

    findings = _run(cbd, sweep)

    assert findings == [], findings
    assert sorted(aws.deleted) == sorted(dead), aws.deleted
    out = capsys.readouterr().out
    assert "age not establishable" not in out, (
        f"{sweep} asked its age gate about a long-lived stack's names: {out}"
    )


def test_every_run_prefix_reason_is_about_a_sweep_that_selects_by_it():
    """The sweeps measured against the run-prefix premise, held to the source.

    `_run_prefix_only_sweeps` reads its list off the reasons, so every reason
    that names the run prefix is measured by the no-live-run case above, and
    that of each such sweep with an age gate by the age-gate case as well; the
    BDA sweep has no age gate, so the first case alone measures it. What keeps
    those reasons true is that each such sweep selects by `_IDP_RUN_PREFIX_RE`
    itself: one that stopped would be measured against a rule it no longer
    applies, and one that never read the set and selected by it without saying
    so would have a reason nothing measures.
    """
    functions = _top_level_functions(ast.parse(HARNESS.read_text(encoding="utf-8")))
    selecting = {
        sweep
        for sweep in _sweeps_that_never_read_the_set()
        if any(
            isinstance(node, ast.Name) and node.id == "_IDP_RUN_PREFIX_RE"
            for node in ast.walk(functions[sweep])
        )
    }
    measured = set(_run_prefix_only_sweeps())

    assert measured, "no sweep is measured against the run-prefix premise"
    assert measured == selecting, (
        f"the sweeps whose reason is the run-prefix rule are {sorted(measured)} and "
        f"the sweeps leaving the set unread that select by _IDP_RUN_PREFIX_RE are "
        f"{sorted(selecting)}; they differ in {sorted(measured ^ selecting)}"
    )


def test_a_listing_of_only_long_lived_names_reads_no_stacks(cbd, monkeypatch, capsys):
    """Nothing in it can be deleted, so a stack enumeration that fails reports nothing.

    `idp-ux-nightly` names its BDA-as-OCR project after itself in every
    commercial-partition deploy, so while it stands the BDA listing is never
    empty, and a sweep that read the stacks for every listing would report
    itself incomplete over that stack's names whenever the read failed. Every
    name `_names_of_a_long_lived_stack` builds for every member is listed, as a
    blueprint and as a project, with each member up and the stack enumeration
    throttled. None carries a run prefix, so the sweep never reads the stacks:
    there is no finding, nothing is deleted, and every name is counted among
    the ones with no run prefix.
    """
    names = _long_lived_names(cbd)
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_listing(names, names),
        stacks=dict.fromkeys(cbd.SWEEP_EXEMPT_STACK_NAMES, "UPDATE_COMPLETE"),
        stack_listing_error=(0, _client_error("Throttling", "Rate exceeded")),
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert findings == [], findings
    assert aws.deleted == [], aws.deleted
    assert aws.calls == ["list_blueprints", "list_data_automation_projects"], aws.calls
    assert _printed_bda_skips(capsys.readouterr().out) == (0, 0, 2 * len(names))


@pytest.mark.parametrize(
    "near_miss",
    [
        pytest.param("idp-0101-12345x", id="letter-for-the-last-digit"),
        pytest.param("idp-0101-abcdef", id="letters-for-the-time"),
        pytest.param("idp-0101-12345", id="one-digit-short"),
        pytest.param("idp-010x-123456", id="letter-in-the-date"),
        pytest.param("idp-0101_123456", id="underscore-for-the-hyphen"),
    ],
)
def test_a_name_that_only_opens_like_a_run_prefix_reads_no_stacks(
    cbd, monkeypatch, capsys, near_miss
):
    """Only a whole `idp-MMDD-HHMMSS` makes the sweep read the account's stacks.

    Every long-lived stack's name opens `idp-u…`, so a listing of those alone
    passes for a gate that reads the stacks whenever a name's fifth character
    is a digit, or whenever one opens `idp-MMDD-`. Here a name that opens like
    a run prefix and falls short of `_IDP_RUN_PREFIX_RE` is listed beside them,
    as a blueprint and as a project, with the stack enumeration throttled. It
    carries no run prefix, so nothing in the listing can be deleted: the stacks
    are not read, there is no finding, and every name is counted among the
    ones with no run prefix.
    """
    names = [*_long_lived_names(cbd), near_miss]
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_listing(names, names),
        stacks=dict.fromkeys(cbd.SWEEP_EXEMPT_STACK_NAMES, "UPDATE_COMPLETE"),
        stack_listing_error=(0, _client_error("Throttling", "Rate exceeded")),
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert cbd._IDP_RUN_PREFIX_RE.match(near_miss) is None, near_miss
    assert findings == [], findings
    assert aws.deleted == [], aws.deleted
    assert aws.calls == ["list_blueprints", "list_data_automation_projects"], aws.calls
    assert _printed_bda_skips(capsys.readouterr().out) == (0, 0, 2 * len(names))


def test_a_failed_stack_enumeration_counts_only_the_names_it_left_undecided(
    cbd, monkeypatch
):
    """Beside a dead run's names, the finding counts those and no long-lived name.

    The dead run's names carry a run prefix, so the stacks are read for them,
    and a throttled read leaves them undecided: nothing is deleted, and the
    sweep reports itself incomplete once. The long-lived names listed beside
    them were never candidates, so the number that finding gives is the dead
    run's alone.
    """
    names = _long_lived_names(cbd)
    dead_blueprints, dead_projects = _bda_resources_named_for(DEAD_RUN)
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_listing(names + dead_blueprints, names + dead_projects),
        stacks=dict.fromkeys(cbd.SWEEP_EXEMPT_STACK_NAMES, "UPDATE_COMPLETE"),
        stack_listing_error=(0, _client_error("Throttling", "Rate exceeded")),
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert aws.deleted == [], aws.deleted
    assert len(findings) == 1, findings
    assert findings[0].startswith(cbd.SWEEP_NOT_COMPLETED), findings
    assert _counted_as_undecided(findings[0]) == len(dead_blueprints) + len(
        dead_projects
    ), findings


def test_a_live_run_listed_beside_the_long_lived_stack_keeps_its_names(
    cbd, monkeypatch, capsys
):
    """The listing every run starts with while that stack stands.

    The long-lived stack's names sit beside a live run's and a dead run's, and
    the stacks answer: the live run's names stand, the dead run's go, and none
    of the long-lived stack's does. So a sweep that skipped the stacks for a
    listing holding any name without a run prefix, and read every run as over,
    fails here on the live run's names rather than only on what it reports.
    The summary line counts the live run's names as names, and that run once,
    as a run.
    """
    names = _long_lived_names(cbd)
    live = RUN_STACKS["primary-run"]
    live_blueprints, live_projects = _bda_resources_named_for(live)
    dead_blueprints, dead_projects = _bda_resources_named_for(DEAD_RUN)
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_listing(
            names + live_blueprints + dead_blueprints,
            names + live_projects + dead_projects,
        ),
        stacks={
            **dict.fromkeys(cbd.SWEEP_EXEMPT_STACK_NAMES, "UPDATE_COMPLETE"),
            live: "CREATE_IN_PROGRESS",
            DEAD_RUN: "DELETE_COMPLETE",
        },
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert findings == [], findings
    assert sorted(aws.deleted) == _arns(dead_blueprints, dead_projects), (
        f"expected exactly the dead run's names to go, got {aws.deleted}"
    )
    assert _printed_bda_skips(capsys.readouterr().out) == (
        len(live_blueprints) + len(live_projects),
        1,
        2 * len(names),
    )


@pytest.mark.parametrize("value", UNDATEABLE)
@pytest.mark.parametrize(
    "sweep", [s for s in _long_lived_sweeps() if _SWEEP_SPECS[s]["age_gate"]]
)
def test_the_long_lived_stack_is_set_aside_before_the_fail_closed_age_gate(
    cbd, monkeypatch, capsys, sweep, value
):
    """Counted as not examined where the listing gives it no usable timestamp.

    Every age gate here fails closed (#1445): an item it cannot date is kept and
    named on a `⚠️ … age not establishable` line. Asking the set first is what
    keeps the long-lived stack's resources off that line, which would otherwise
    report resources the sweep never deletes on every run whose listing left
    them undated. The old run beside them keeps its date and is still reaped,
    so a sweep that kept everything undated, or everything, fails here as well.
    """
    owners = [LONG_LIVED_STACK, OLD_RUN_STACK]
    field = _SWEEP_SPECS[sweep]["age_gate"]["field"]
    aws, s3 = _install_beside_long_lived(
        cbd,
        monkeypatch,
        sweep,
        owners,
        contents=_undated_for(_listing(sweep, owners), field, value, LONG_LIVED_STACK),
    )

    findings = _run(cbd, sweep)

    assert findings == [], findings
    assert sorted(aws.deleted) == sorted(_resources_of(sweep, OLD_RUN_STACK)), (
        f"{sweep} deleted {aws.deleted}; expected exactly the old run's "
        f"{_resources_of(sweep, OLD_RUN_STACK)}"
    )
    if sweep == "cleanup_stale_idp_buckets":
        assert s3.emptied == _resources_of(sweep, OLD_RUN_STACK), s3.emptied
    out = capsys.readouterr().out
    assert "age not establishable" not in out, (
        f"{sweep} named the long-lived stack's undated resources as items whose "
        f"age it could not establish, so it asked its age gate before the set: "
        f"{out}"
    )
    assert _printed_long_lived_count(out) == len(
        _resources_of(sweep, LONG_LIVED_STACK)
    ), out


def test_the_stack_sweep_does_not_count_the_long_lived_stacks_nested_stacks(
    cbd, monkeypatch, capsys
):
    """Passed over by the top-level filter, as every nested stack is.

    A nested stack is named `<parent>-<LogicalId>-<suffix>`, and the stack
    sweep's match is the long-lived stack and its `-iam` pre-stack only, so it
    is that filter rather than the set that keeps these: one that reached the
    match would be reaped with the old run. Nor are they counted, so the count
    stays one of top-level stacks like every other on that line. The old run's
    nested stacks are passed over the same way, and its top-level stacks are
    still reaped.
    """
    sweep = "cleanup_stale_idp_stacks"
    owners = [LONG_LIVED_STACK, OLD_RUN_STACK]
    old = datetime.now(tz=timezone.utc) - timedelta(seconds=STALE_AGE_SECONDS)
    contents = _listing(sweep, owners)
    contents["list_stacks"][0]["StackSummaries"] += [
        {
            "StackName": f"{owner}-{logical_id}-1A2B3C4D5E6F",
            "CreationTime": old,
            "RootId": f"arn:fake:stack/{owner}/root",
            "ParentId": f"arn:fake:stack/{owner}/root",
        }
        for owner in owners
        for logical_id in ("PATTERNSTACK", "APIRESOLVERSTACK")
    ]
    aws, _ = _install_beside_long_lived(
        cbd, monkeypatch, sweep, owners, contents=contents
    )

    findings = cbd.cleanup_stale_idp_stacks()

    assert findings == [], findings
    assert sorted(aws.deleted) == sorted(_resources_of(sweep, OLD_RUN_STACK)), (
        aws.deleted
    )
    assert _printed_long_lived_count(capsys.readouterr().out) == len(
        _resources_of(sweep, LONG_LIVED_STACK)
    ), "the long-lived stack's nested stacks were counted as set aside for it"


def test_a_top_level_stack_named_after_the_long_lived_one_is_still_reaped(
    cbd, monkeypatch, capsys
):
    """`idp-ux-nightly-test` and its pre-stack go; the long-lived pair stands.

    All of them are top-level and older than the age gate. The long-lived stack
    and `idp-ux-nightly-iam` are counted as set aside for it, and the throwaway pair
    is reaped with the old run, so a sweep that spared every `idp-ux-nightly-<x>`
    fails here, and so does one that stopped sparing either of the two names it
    keeps.
    """
    sweep = "cleanup_stale_idp_stacks"
    owners = [LONG_LIVED_STACK, OLD_RUN_STACK]
    throwaway = [f"{LONG_LIVED_STACK}-test", f"{LONG_LIVED_STACK}-test-iam"]
    old = datetime.now(tz=timezone.utc) - timedelta(seconds=STALE_AGE_SECONDS)
    contents = _listing(sweep, owners)
    contents["list_stacks"][0]["StackSummaries"] += [
        {"StackName": name, "CreationTime": old} for name in throwaway
    ]
    aws, _ = _install_beside_long_lived(
        cbd, monkeypatch, sweep, owners, contents=contents
    )

    findings = cbd.cleanup_stale_idp_stacks()

    assert findings == [], findings
    reaped = [*_resources_of(sweep, OLD_RUN_STACK), *throwaway]
    assert sorted(aws.deleted) == sorted(reaped), (
        f"the stack sweep deleted {aws.deleted}; expected exactly {reaped}. "
        f"{throwaway[0]} is a top-level stack of its own, and anything of "
        f"{LONG_LIVED_STACK} or {LONG_LIVED_STACK}-iam is the long-lived stack lost"
    )
    assert _printed_long_lived_count(capsys.readouterr().out) == len(
        _resources_of(sweep, LONG_LIVED_STACK)
    ), f"{throwaway} was counted as set aside for the long-lived stack"


@pytest.mark.parametrize("marker", ["future", "expired", "unreadable"])
def test_the_long_lived_stack_is_set_aside_before_its_retention_marker_is_read(
    cbd, monkeypatch, capsys, marker
):
    """A marker in its name neither keeps it nor reaps it.

    The stack sweep reads a run's retention marker before its age gate: one in
    the future keeps the stack as retained, an expired one reaps it at any age,
    and one whose read did not answer keeps it on a ⚠️ line of its own.
    `retain_failed_stack` writes a marker only for a run's own stack, so none
    names this one today; the case pins the order the docstring states, under
    which the stack is counted as not examined whatever the marker says. An expired
    marker is still dropped, as every expired marker is, so it holds no
    retention slot. The old run beside it has no marker and is still reaped.
    """
    sweep = "cleanup_stale_idp_stacks"
    owners = [LONG_LIVED_STACK, OLD_RUN_STACK]
    key = f"{cbd.RETENTION_S3_PREFIX}/{LONG_LIVED_STACK}.json"
    contents = _listing(sweep, owners)
    contents["list_objects_v2"] = [{"Contents": [{"Key": key}]}]
    aws, _ = _install_beside_long_lived(
        cbd, monkeypatch, sweep, owners, contents=contents
    )
    monkeypatch.setenv("SOURCE_BUCKET", "a-bucket")
    now = datetime.now(tz=timezone.utc)
    keep_until = {"future": now + timedelta(days=1), "expired": now - timedelta(days=1)}

    def _get_object(Bucket, Key):  # noqa: N803 - boto3 casing
        if marker == "unreadable":
            raise _client_error("AccessDenied", "not authorized", "GetObject")
        body = json.dumps({"keep_until": keep_until[marker].isoformat()})
        return {"Body": io.BytesIO(body.encode())}

    monkeypatch.setattr(aws, "get_object", _get_object, raising=False)

    findings = cbd.cleanup_stale_idp_stacks()

    assert findings == [], findings
    dropped = [d for d in aws.deleted if d.startswith(f"{cbd.RETENTION_S3_PREFIX}/")]
    reaped = [d for d in aws.deleted if d not in dropped]
    assert sorted(reaped) == sorted(_resources_of(sweep, OLD_RUN_STACK)), (
        f"with a {marker} marker for {LONG_LIVED_STACK} the sweep deleted {aws.deleted}"
    )
    assert dropped == ([key] if marker == "expired" else []), dropped
    out = capsys.readouterr().out
    assert "retention marker could not be read" not in out, out
    assert "retained idp- stack(s) skipped" not in out, out
    assert _printed_long_lived_count(out) == len(
        _resources_of(sweep, LONG_LIVED_STACK)
    ), f"a {marker} marker took {LONG_LIVED_STACK} out of the long-lived count: {out}"


# ---------------------------------------------------------------------------
# The retry config the layer's safety premise rests on, per client (#1445)
#
# The module header's claim about `_THROTTLE_RETRY_CONFIG` is load-bearing
# rather than decorative: "what reaches a handler is a durable condition" is
# the premise under which every fail-closed handler above may treat an error as
# meaningful. Several client constructions on the sweep path were built without
# it, so a single `Rate exceeded` arrived at those handlers indistinguishable
# from a denial. Two had named consequences — the retention-marker read, whose
# failure narrows the protection a path that DELETES is applied with, and
# `_live_idp_run_prefixes`, whose failure is the enumeration finding for the
# bucket AND the policy sweep, both registered `fails_run=True`, so one
# transient CloudFormation throttle ended a night's run red with nothing else
# wrong.
#
# Asserted PER CLIENT and over a DERIVED set, for two separate reasons.
#
# Per client, because any aggregate form of this — "a client carried it", "an
# adaptive Config was built" — passes while an individual client is unprotected,
# and that is precisely the state the header described wrongly for as long as it
# stood. Each construction site is its own case and names its own helper.
#
# Derived, because the ones #1445 reported were found by reading the file — it
# reported fewer than there are — and the next client added under a sweep is the
# same defect again. `_client_sites()` walks the call graph from the registered
# sweeps, so a construction anywhere in that closure is covered without being
# listed here, and `CLIENT_SITES_FLOOR` pins the sites the derivation found when
# it was written, so a derivation that silently stops reaching one fails rather
# than parametrising fewer cases. The retention WRITE is the one member of the
# floor with no call edge from a sweep — the sweeps read what it writes — so it
# is reached through `RETENTION_WRITE_ROOTS` and asserted like the rest.
# ---------------------------------------------------------------------------

#: The harness's one shared retry Config, by name. A site must NAME it rather
#: than build its own: a local Config could satisfy a mode-and-count check while
#: diverging from every other client the next time the shared one is tuned, and
#: the header's claim is literally about this constant.
RETRY_CONFIG_NAME = "_THROTTLE_RETRY_CONFIG"

#: The attempt floor, the same one #1444 asserted for the liveness client. A
#: floor rather than the exact 10 so tuning the constant up is not a test edit.
MIN_RETRY_ATTEMPTS = 5

#: One root that is NOT a Step 0 sweep, so it is named rather than derived.
#: `retain_failed_stack` is the WRITE side of the retention markers
#: `cleanup_stale_idp_stacks` reads: a throttled `put_object` here returns False,
#: the run tears its own stack down, and the evidence the retention exists to
#: preserve is gone. There is no call edge from a sweep to find it by — the
#: sweeps read what this writes — so the root is stated.
RETENTION_WRITE_ROOTS = ("retain_failed_stack",)


def _top_level_functions(tree):
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _called_function_names(node):
    """The call-graph edges out of `node` that stay inside this module.

    Bare `Name` calls only, which is how every helper here is called. An
    attribute call (`cf.delete_stack`) is a call into boto3, not an edge.
    """
    return {
        child.func.id
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
    }


def _retry_closure():
    """(functions, {name: path}) for every function reachable from a root.

    The path is kept because it is what a failure has to print: "this helper
    builds an unprotected client" is not actionable without the sweep that
    reaches it, and the two can be several calls apart (the VPC reaper reaches
    its ENI sweep through `delete_apigw_test_vpc`).
    """
    functions = _top_level_functions(ast.parse(HARNESS.read_text(encoding="utf-8")))
    paths = {}
    queue = [(name, (name,)) for name in (*_sweep_names(), *RETENTION_WRITE_ROOTS)]
    while queue:
        name, path = queue.pop(0)
        if name in paths or name not in functions:
            continue
        paths[name] = path
        for callee in sorted(_called_function_names(functions[name])):
            if callee in functions and callee not in paths:
                queue.append((callee, (*path, callee)))
    return functions, paths


def _client_sites():
    """Every `boto3.client`/`boto3.resource` construction in that closure.

    Returns (helper, kind, service, lineno, config_expr, path) per site, with
    `config_expr` the source of the `config=` keyword or None where there is
    none — which is the whole finding.
    """
    functions, paths = _retry_closure()
    sites = []
    for name in sorted(paths):
        for node in ast.walk(functions[name]):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr in ("client", "resource")
                and isinstance(func.value, ast.Name)
                and func.value.id == "boto3"
            ):
                continue
            service = (
                node.args[0].value
                if node.args and isinstance(node.args[0], ast.Constant)
                else "<dynamic>"
            )
            config = next(
                (ast.unparse(kw.value) for kw in node.keywords if kw.arg == "config"),
                None,
            )
            sites.append(
                (
                    name,
                    func.attr,
                    service,
                    node.lineno,
                    config,
                    " -> ".join(paths[name]),
                )
            )
    return tuple(sites)


SWEEP_CLIENT_SITES = _client_sites()

#: (helper, kind, service) for every site the derivation found when this was
#: written: those reachable from a registered sweep, plus the retention writer.
#: A floor, not a ceiling — a new site is covered by the parametrisation above
#: without being added here — and it is what stops the derivation going quietly
#: vacuous. Some of these carried no config at all before #1445 while the rest
#: already did, which is the mixture that let the header's claim read as true;
#: which ones is `git log -p` on the module, not a tally here that would be
#: stale the next time a client is added.
#:
#: Named for what this holds rather than for what it would turn off. It widens
#: what the gate demands instead of narrowing it, so the scanner that collects
#: gate carve-outs by name has nothing to collect here and a registry entry could
#: say nothing useful about it.
CLIENT_SITES_FLOOR = frozenset(
    {
        ("_drop_retention_marker", "client", "s3"),
        ("_expired_retention_markers", "client", "s3"),
        ("_force_delete_vpc_stack_enis", "client", "cloudformation"),
        ("_force_delete_vpc_stack_enis", "client", "ec2"),
        ("_live_idp_run_prefixes_in", "client", "cloudformation"),
        ("cleanup_stale_apigw_test_vpcs", "client", "cloudformation"),
        ("cleanup_stale_bda_blueprints", "client", "bedrock-data-automation"),
        ("cleanup_stale_ci_diagnostics", "client", "s3"),
        ("cleanup_stale_idp_buckets", "client", "s3"),
        ("cleanup_stale_idp_buckets", "resource", "s3"),
        ("cleanup_stale_idp_policies", "client", "iam"),
        ("cleanup_stale_idp_stacks", "client", "cloudformation"),
        ("delete_apigw_test_vpc", "client", "cloudformation"),
        ("retain_failed_stack", "client", "s3"),
    }
)


@pytest.mark.parametrize(
    ("helper", "kind", "service", "lineno", "config", "path"),
    SWEEP_CLIENT_SITES,
    ids=[f"{s[0]}-{s[1]}-{s[2]}" for s in SWEEP_CLIENT_SITES],
)
def test_every_client_on_a_sweep_path_carries_the_shared_retry_config(
    helper, kind, service, lineno, config, path
):
    """One case per construction site, so losing one names the helper that lost it.

    The header's inference is what this protects. A handler reached by a
    throttle on an unprotected client has to decide between "the account refused
    this" and "ask again in a moment" with nothing to decide on, and every
    handler in this layer resolves that the way the durable reading implies.
    """
    assert config is not None, (
        f"{helper} builds boto3.{kind}({service!r}) at line {lineno} with no "
        f"config, and it is reached as {path}. A throttle on that client "
        f"arrives at a handler that reads an error as a durable condition, "
        f"which is the premise the module header states and this breaks"
    )
    assert config == RETRY_CONFIG_NAME, (
        f"{helper}'s boto3.{kind}({service!r}) at line {lineno} passes "
        f"config={config} rather than {RETRY_CONFIG_NAME}. A second Config on "
        f"this path is a second thing to tune: route it through the shared "
        f"constant so every client in the layer rides out the same burst"
    )


def test_the_derived_client_set_still_covers_every_site_it_was_written_against():
    """Non-vacuity, and it is the half that keeps the derivation honest.

    The parametrisation above is only as strong as what the walk reaches. A
    helper renamed, a call edge removed, or a root dropped would narrow it
    silently and every remaining case would stay green — the shape of the defect
    this module exists for, one level up.
    """
    found = {
        (helper, kind, service) for helper, kind, service, *_ in SWEEP_CLIENT_SITES
    }
    missing = sorted(CLIENT_SITES_FLOOR - found)

    assert not missing, (
        f"the call-graph walk no longer reaches {missing}, so those clients are "
        f"asserted by nothing. Either the helper moved (re-derive the set) or a "
        f"root is missing from STARTUP_SWEEPS / RETENTION_WRITE_ROOTS"
    )


def test_the_shared_retry_config_rides_out_a_burst_rather_than_merely_existing(cbd):
    """What the sites above name has to actually be adaptive and deep.

    Every static assertion in this section compares a NAME. Pointing every site
    at a constant rebuilt as `standard`/3 attempts would satisfy every one of
    them while putting the whole throttle class back in front of the handlers,
    so the object itself is read here — the shape #1444 used for the liveness
    client.
    """
    cfg = cbd._THROTTLE_RETRY_CONFIG

    assert cfg.retries["mode"] == "adaptive", (
        f"the shared retry config is {cfg.retries}. Only adaptive mode backs off "
        f"on the client-side rate estimate, which is what rides out an "
        f"account-wide burst rather than retrying into it"
    )
    assert cfg.retries["max_attempts"] >= MIN_RETRY_ATTEMPTS, (
        f"the shared retry config allows {cfg.retries.get('max_attempts')} "
        f"attempts, below the floor of {MIN_RETRY_ATTEMPTS}; botocore's default "
        f"of 4 is what a probe already died at"
    )


#: The three helpers whose client the issue names as a live decision path, with
#: a call that builds it. Driven for real because the static check above reads a
#: NAME at a call site and this reads the Config OBJECT the call actually passes
#: — a constant shadowed locally, or a site reaching a stale copy, satisfies one
#: and not the other.
_RETRY_DRIVES = {
    "_live_idp_run_prefixes": lambda cbd: cbd._live_idp_run_prefixes(),
    "_expired_retention_markers": lambda cbd: cbd._expired_retention_markers(
        "a-bucket", datetime.now(tz=timezone.utc)
    ),
    "_drop_retention_marker": lambda cbd: cbd._drop_retention_marker(
        "a-bucket", DEAD_RUN
    ),
}


@pytest.mark.parametrize("helper", sorted(_RETRY_DRIVES))
def test_the_config_each_live_path_helper_actually_passes_is_adaptive(
    cbd, monkeypatch, helper
):
    """Per helper, on the Config the running code hands to boto3."""
    seen = []
    aws = _FakeAws(
        {
            "list_stacks": [{"StackSummaries": []}],
            "list_objects_v2": [{"Contents": []}],
        }
    )

    def _client(name, *a, **k):
        seen.append((name, k.get("config")))
        return aws

    monkeypatch.setattr(cbd.boto3, "client", _client)

    _RETRY_DRIVES[helper](cbd)

    assert seen, f"{helper} built no client at all, so this asserts nothing"
    for service, cfg in seen:
        assert cfg is not None, (
            f"{helper} built its {service} client without a Config, so a "
            f"throttle reaches its caller as a durable condition"
        )
        assert cfg.retries["mode"] == "adaptive", (helper, service, cfg.retries)
        assert cfg.retries["max_attempts"] >= MIN_RETRY_ATTEMPTS, (
            helper,
            service,
            cfg.retries,
        )
