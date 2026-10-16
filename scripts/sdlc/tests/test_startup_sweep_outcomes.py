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
#: covers half the surface. The BDA sweep makes THREE, and reads the same
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
# bucket, role and policy reapers read — and the three answers stay apart: the
# run is live, the run is over, or the enumeration did not complete. Only the
# second deletes.
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
    assert aws.calls.count("list_stacks") == 1, aws.calls
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
        "list_stacks",
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
        ("_live_idp_run_prefixes", "client", "cloudformation"),
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
