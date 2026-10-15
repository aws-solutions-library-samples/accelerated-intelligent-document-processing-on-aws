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

import pytest
from botocore.exceptions import ClientError

pytestmark = pytest.mark.unit

HARNESS = Path(__file__).resolve().parents[1] / "codebuild_deployment.py"

#: Older than every age gate in the module (the widest is the 30-day diagnostics
#: retention window), so one constant serves all six sweeps.
STALE_AGE_SECONDS = 31 * 24 * 3600

#: A run prefix whose stacks are all gone, so nothing protects its leftovers.
DEAD_RUN = "idp-0101-000000"

#: A second run prefix, for the mixed case: one run whose liveness is readable
#: beside one whose is not.
OTHER_RUN = "idp-0202-000000"


def _client_error(code, message, operation="DescribeStacks"):
    """A REAL botocore `ClientError` carrying a real error code.

    Real rather than a stand-in, for two reasons that both make the assertions
    below vacuous if it is faked. The fix turns on reading `Error.Code` off the
    payload, so a double raising a bare `Exception`, or a `ClientError` built
    with an empty `response`, cannot distinguish the branches it is testing. And
    the handler this replaces named a CLIENT-BOUND exception class,
    `cf_client.exceptions.ClientError`: botocore's error factory sets that
    attribute to `botocore.exceptions.ClientError` itself
    (`botocore/errorfactory.py`), so this is the one object both the old handler
    and the new one actually catch. Raise anything else and the old code misses
    its own `except`, the exception lands in the sweep's outer handler, a finding
    comes back — and the test goes green against the defect.
    """
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


#: What CloudFormation answers for a stack that genuinely is not there. The code
#: is stated by the `DescribeStacks` API reference ("If the stack doesn't exist,
#: a ValidationError is returned") and carried in botocore's own service model
#: documentation string for the operation; the message wording is not in the API
#: reference, and is the shape moto reproduces (`moto/cloudformation/
#: exceptions.py`) and the shape two call sites in this repository already match
#: on.
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


class _FakeClientExceptions:
    """`client.exceptions` for the fake, carrying the real `ClientError`.

    The production handler for the per-prefix liveness check used to name
    `cf_client.exceptions.ClientError`. Without this attribute that expression
    raises `AttributeError` while the original exception is being handled, the
    sweep's outer `except Exception` catches that instead, and a finding comes
    back for the wrong reason — so the pre-fix run would look correct. The
    attribute is botocore's own class, which is what a real client exposes.
    """

    ClientError = ClientError


class _FakeAws:
    """Account contents plus a chosen failure, standing in for every client."""

    exceptions = _FakeClientExceptions()

    def __init__(
        self,
        contents=None,
        failing=None,
        breaking=None,
        stack_errors=None,
        stack_statuses=None,
    ):
        self.contents = contents or {}
        self.failing = failing
        self.breaking = breaking
        #: {StackName: (code, message)} — what `describe_stacks` raises for that
        #: name, as a real ClientError. The per-prefix liveness question, which
        #: is a different surface from `failing`: these calls are answered, with
        #: an error that may or may not establish the run is over.
        self.stack_errors = stack_errors or {}
        #: {StackName: StackStatus}, for a run that answers as live.
        self.stack_statuses = stack_statuses or {}
        self.deleted = []
        self.described = []

    def _check(self, api):
        if self.failing == api:
            raise RuntimeError(f"AccessDenied: {api}")

    # --- enumerations -----------------------------------------------------
    def get_paginator(self, api):
        self._check(api)
        return _FakePaginator(self.contents.get(api, [{}]))

    def list_data_automation_projects(self):
        self._check("list_data_automation_projects")
        return {"projects": self.contents.get("projects", [])}

    def list_buckets(self):
        self._check("list_buckets")
        return {"Buckets": self.contents.get("buckets", [])}

    def describe_stacks(self, StackName):  # noqa: N803 - boto3 casing
        self._check("describe_stacks")
        self.described.append(StackName)
        if StackName in self.stack_errors:
            code, message = self.stack_errors[StackName]
            raise _client_error(code, message)
        # Terminal by default, so the BDA sweep reads the run as over and its
        # blueprints as stale. A describe that cannot answer is a different
        # question, driven through `stack_errors`.
        status = self.stack_statuses.get(StackName, "DELETE_COMPLETE")
        return {"Stacks": [{"StackStatus": status}]}

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
#: covers half the surface. Every registered sweep needs an entry here; one
#: without is a failure, not a skip.
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
        "enumerations": ("list_blueprints", "list_data_automation_projects"),
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
    stack_errors=None,
    stack_statuses=None,
):
    """Point the harness at the fake account and return it."""
    aws = _FakeAws(
        contents if contents is not None else _contents(cbd, sweep, with_target),
        failing,
        breaking,
        stack_errors=stack_errors,
        stack_statuses=stack_statuses,
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
# The BDA sweep's PER-PREFIX liveness check: "gone" and "could not tell" are
# different answers (#1440)
#
# This is the one check in the six that asks a question per item on a path that
# deletes: for each run prefix behind a blueprint or project, is that run's
# CloudFormation stack still there? `describe_stacks` raises `ClientError` for a
# stack that genuinely is not there AND for `Throttling`, `AccessDenied` and
# anything else, and the handler used to record every one of them as "this run
# has no active stack" — which is the answer that permits the delete.
#
# ⚠️ The failure mode coincides with the condition the check exists to guard: a
# `DescribeStacks` is throttled when several runs are in flight, which is exactly
# when a blueprint belongs to a run that is still executing. Nothing here claims
# a blueprint has ever actually been lost this way; it is a latent direction,
# fixed in the direction the rest of this module already takes.
#
# Four directions, and the second is the one a careless fix breaks: protecting
# everything makes the reaper permanently inert, which is #1434's defect pointed
# the other way.
# ---------------------------------------------------------------------------


def _bda_contents(*run_prefixes):
    """Account contents holding one LIVE blueprint per run prefix."""
    return {
        "list_blueprints": [
            {
                "blueprints": [
                    {
                        "blueprintName": prefix,
                        "blueprintArn": f"arn:fake:blueprint/{prefix}",
                    }
                    for prefix in run_prefixes
                ]
            }
        ],
        "projects": [],
    }


#: Error codes that do NOT establish the run is over, so each must leave the
#: prefix in place. The last two are the point of the list rather than padding:
#: an `AccessDenied` is a durable condition that answers nothing about the
#: account, and an unrecognised code has to take the same branch, since the safe
#: direction is the one an unfamiliar condition must fall into.
UNANSWERED_LIVENESS_ERRORS = (
    ("Throttling", "Rate exceeded"),
    ("ThrottlingException", "Rate exceeded"),
    ("RequestLimitExceeded", "Request limit exceeded"),
    ("AccessDenied", "User is not authorized to perform cloudformation:DescribeStacks"),
    ("SomeCodeNobodyHasSeenYet", "an answer this code has never been read against"),
    # A ValidationError whose message is about the REQUEST, not the stack's
    # existence. `ValidationError` is CloudFormation's catch-all 400, so the code
    # alone cannot carry the claim.
    ("ValidationError", "1 validation error detected: Value '' at 'stackName'"),
)


@pytest.mark.parametrize(
    ("code", "message"), UNANSWERED_LIVENESS_ERRORS, ids=lambda v: str(v)[:40]
)
def test_an_unanswered_liveness_check_protects_the_prefix(
    cbd, monkeypatch, capsys, code, message
):
    """Direction 1: could not tell → nothing deleted, and the sweep says so."""
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_contents(DEAD_RUN),
        stack_errors={DEAD_RUN: (code, message)},
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert aws.described == [DEAD_RUN], "the liveness check was never attempted"
    assert aws.deleted == [], (
        f"a {code} on the liveness check deleted {aws.deleted} — the blueprint "
        f"of a run whose stack this sweep could not read, which on a throttle is "
        f"a run that is most likely still executing"
    )
    assert findings, (
        f"a {code} left the prefix unexamined and the sweep reported nothing, so "
        f"the run cannot tell this apart from a sweep that established every "
        f"run was over"
    )
    assert all(f.startswith(cbd.SWEEP_NOT_COMPLETED) for f in findings), findings
    assert DEAD_RUN in findings[0], (
        f"the finding does not name the prefix it could not establish: {findings}"
    )
    assert code in findings[0], (
        f"the finding does not carry the error code, so a reader cannot tell a "
        f"throttle from a denial: {findings}"
    )
    assert "No stale BDA resources found" not in capsys.readouterr().out


def test_a_stack_that_genuinely_does_not_exist_is_still_reaped(
    cbd, monkeypatch, capsys
):
    """Direction 2: the half a fix that protects everything breaks.

    A `ValidationError` naming a stack that is not there is a positive answer —
    the run is over — and its blueprints are exactly what this reaper exists to
    take. Protecting those too would make the sweep permanently inert, which is
    #1434's defect pointed the other way, and it would pass direction 1.
    """
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_contents(DEAD_RUN),
        stack_errors={DEAD_RUN: STACK_GONE},
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert aws.deleted == [f"arn:fake:blueprint/{DEAD_RUN}"], (
        "the run's stack is gone and its blueprint was left behind — the reaper "
        "is inert"
    )
    assert findings == [], f"a completed sweep reported {findings}"
    assert cbd.SWEEP_NOT_COMPLETED not in capsys.readouterr().out


def test_a_live_stack_protects_its_blueprints_and_is_not_a_finding(cbd, monkeypatch):
    """Direction 3: an answered liveness check is a COMPLETED sweep.

    Nothing to reap because the run is still going is the quiet path, and a
    reaper that reports a problem on every green deploy trains the reader to
    ignore the row.
    """
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_contents(DEAD_RUN),
        stack_statuses={DEAD_RUN: "CREATE_IN_PROGRESS"},
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert aws.deleted == []
    assert findings == [], f"a live run is not a sweep failure, yet got {findings}"


def test_one_unanswered_prefix_does_not_make_the_whole_sweep_inert(cbd, monkeypatch):
    """Direction 4: per prefix, not per sweep.

    The protection is scoped to the prefix whose question went unanswered. A
    reaper that gave up on the first throttle would stop reaping on exactly the
    busy nights that leak the most, and would report the same finding either way
    — so the finding alone does not establish this.
    """
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_contents(DEAD_RUN, OTHER_RUN),
        stack_errors={
            DEAD_RUN: STACK_GONE,
            OTHER_RUN: ("ThrottlingException", "Rate exceeded"),
        },
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert aws.deleted == [f"arn:fake:blueprint/{DEAD_RUN}"], (
        f"expected only the established-gone run's blueprint to be taken, got "
        f"{aws.deleted}"
    )
    assert findings and OTHER_RUN in findings[0], findings
    assert DEAD_RUN not in findings[0], (
        f"the finding names a prefix the sweep DID establish, which sends a "
        f"reader after a run that is genuinely over: {findings}"
    )


def test_the_liveness_client_rides_out_the_throttle_class_before_it_decides(
    cbd, monkeypatch
):
    """The throttle should not reach the decision at all.

    The adaptive config is the first half of this fix and the handler is the
    second; with the retry removed a single `Rate exceeded` reaches a handler
    that then has to make a judgement it cannot make well. Asserted on the
    Config the client is built with, the way the IAM-burst equivalent in
    `test_variant_probes.py` is.
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


def test_a_partial_liveness_sweep_is_a_warning_row_and_not_a_red_run(cbd):
    """The verdict the registry takes for this sweep, applied to the new finding.

    `fails_run=False` was decided for an INERT sweep, and this finding means a
    prefix was left in place — the same unmeasured leak, so the same verdict. The
    row still has to render, because a protection that reports nothing is how the
    reaper's silence was a problem in the first place.
    """
    entry = next(
        s for s in cbd.STARTUP_SWEEPS if s.func == "cleanup_stale_bda_blueprints"
    )
    assert entry.fails_run is False

    finding = f"{cbd.SWEEP_NOT_COMPLETED} could not establish whether {DEAD_RUN}"
    text = _summary(cbd, {"cleanup_stale_bda_blueprints": [finding]})

    assert "OVERALL: PASS" in text
    row = _row_for(cbd, text, "cleanup_stale_bda_blueprints")
    assert row.startswith("⚠️"), row
    assert DEAD_RUN in text, "the prefix left in place is not named in the table"


def test_two_unanswered_prefixes_are_both_named_and_counted(cbd, monkeypatch):
    """The finding is per prefix, so the reader gets the whole list.

    A count with one example would send somebody looking at one run while
    another was left in the same state, and the next run's output is the only
    other place that pairing exists.
    """
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_contents(DEAD_RUN, OTHER_RUN),
        stack_errors={
            DEAD_RUN: ("Throttling", "Rate exceeded"),
            OTHER_RUN: ("AccessDenied", "not authorized"),
        },
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert aws.deleted == []
    assert len(findings) == 1, findings
    assert DEAD_RUN in findings[0] and OTHER_RUN in findings[0], findings
    assert "2" in findings[0], (
        f"the count of unanswered prefixes is missing: {findings}"
    )
    assert "; " in findings[0], (
        f"two prefixes are run together in one unreadable string: {findings}"
    )


def test_a_describe_that_answers_with_no_stack_protects_the_prefix(cbd, monkeypatch):
    """A response shape this code cannot read is not an absence it can act on.

    `DescribeStacks` asked about a named stack either answers with that stack or
    raises, so an empty `Stacks` list is not a documented answer at all. Reading
    it as "no stack, therefore the run is over" would put a shape nobody has
    seen on the branch that deletes.
    """
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_contents(DEAD_RUN),
    )
    monkeypatch.setattr(aws, "describe_stacks", lambda StackName: {"Stacks": []})  # noqa: N803

    findings = cbd.cleanup_stale_bda_blueprints()

    assert aws.deleted == []
    assert findings and DEAD_RUN in findings[0], findings
    assert "no stack" in findings[0], (
        f"the finding does not say what was wrong with the answer: {findings}"
    )


def test_an_exception_that_is_not_a_client_error_also_protects_the_prefix(
    cbd, monkeypatch
):
    """A connection failure or an expired credential carries no error code.

    There is nothing to read a code off, so there is nothing that could
    establish the run is over — and the detail has to name the exception type,
    since that is the only thing a reader has to go on.
    """
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_contents(DEAD_RUN),
    )

    def _boom(StackName):  # noqa: N803 - boto3 casing
        raise ConnectionError("could not reach the endpoint")

    monkeypatch.setattr(aws, "describe_stacks", _boom)

    findings = cbd.cleanup_stale_bda_blueprints()

    assert aws.deleted == []
    assert findings and DEAD_RUN in findings[0], findings
    assert "ConnectionError" in findings[0], (
        f"the finding names neither a code nor an exception type: {findings}"
    )


def test_a_name_with_no_run_prefix_is_never_reaped(cbd, monkeypatch):
    """A name that carries no `idp-MMDD-HHMMSS` prefix has no stack to ask about.

    No liveness question is asked for it, so nothing could have established that
    its run is over, and the same rule applies: no answer, no delete. The
    direction matters because this one is not even reported — it is skipped
    silently — so if it were reaped it would be reaped with no check at all.
    """
    aws = _install(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        with_target=True,
        contents=_bda_contents("idp-weird"),
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert aws.described == [], "a prefixless name should ask nothing of CloudFormation"
    assert aws.deleted == [], f"a name with no run prefix was reaped: {aws.deleted}"
    assert findings == []


# ---------------------------------------------------------------------------
# The long-lived stack five of the sweeps leave standing
#
# `idp-ux-nightly` outlives every run, and each sweep would take it its own way.
# The stack sweep reaps it and its `-iam` pre-stack once they are older than its
# age gate. The bucket sweep empties and deletes its buckets once they are older
# than its own, with the stack still up, because its live-stack protection is
# read through a run's `idp-MMDD-HHMMSS` prefix and this name carries none. The
# BDA sweep asks after the stack that a name's first three hyphen-separated
# parts spell, which for `idp-ux-nightly-<x>` is this one, so it takes those
# while the stack sits in a status outside the active set; it takes the stack's
# BDA-as-OCR project, `idp-ux-nightly_OCR_StdOutput`, at any status, because
# those parts of that name spell no stack at all. The role and policy sweeps
# only delete run-prefixed names, so they never matched this stack's roles or
# policies, and consult the set anyway so that does not rest on the pattern.
#
# Each sweep with an age gate asks the set before it. The gates fail closed
# (#1445), so an item a listing did not date is kept and named as one whose age
# could not be established; the long-lived stack's resources are counted as its
# instead. The stack sweep also asks it before a run's retention marker, and
# after passing over nested stacks, so it counts top-level stacks only.
#
# Every case lists the long-lived stack's resources BESIDE another stack's and
# asserts that other stack is still reaped: a sweep that stopped reaping
# altogether would pass a case holding the long-lived stack alone.
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


def _long_lived_sweeps():
    """The five sweeps that consult the set, by name."""
    return [
        "cleanup_stale_idp_stacks",
        "cleanup_stale_idp_buckets",
        "cleanup_stale_idp_roles",
        "cleanup_stale_idp_policies",
        "cleanup_stale_bda_blueprints",
    ]


def _resources_of(sweep, owner):
    """What `sweep` deletes of a stack named `owner`, as `aws.deleted` records it.

    Stacks, buckets and roles are deleted by name; managed policies, BDA
    projects and blueprints by ARN, and `_listing` reads their names off the
    ARN's last part. The roles are the `-iam` pre-stack's service role and the
    two `DeletionPolicy: Retain` leaves behind in the knowledge-base stack. The
    BDA projects are a versioned one, named `<stack>-<version>`, and the
    BDA-as-OCR one, named `<stack>_OCR_StdOutput`.
    """
    if sweep == "cleanup_stale_idp_stacks":
        return [owner, f"{owner}-iam"]
    if sweep == "cleanup_stale_idp_buckets":
        return [f"{owner}-inputbucket-abc"]
    if sweep == "cleanup_stale_idp_roles":
        return [
            f"{owner}-iam-CFServiceRole",
            f"{owner}-DOCUMENTK-StartIngestionJobFunction-abc",
            f"{owner}-DOCUMENTKB--DataSourceSchedulerRole-xyz",
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
    """Account contents holding what `sweep` reaps of each stack in `owners`.

    The long-lived stack is up, so it and its `-iam` pre-stack are in the
    `list_stacks` the bucket, role and policy sweeps read their live-stack
    protection from. Every other owner's stacks are gone, except from the stack
    sweep's own listing, where leftover stacks are the targets. All of it is
    older than every age gate.
    """
    old = datetime.now(tz=timezone.utc) - timedelta(seconds=STALE_AGE_SECONDS)
    found = [item for owner in owners for item in _resources_of(sweep, owner)]
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
    """How many resources the sweep said it left for a long-lived stack, if any."""
    match = re.search(r"(\d+) named for a long-lived stack", out)
    return int(match.group(1)) if match else None


def test_the_long_lived_stack_set_is_pinned(cbd):
    """Count-pinned: exactly the one stack it was registered with.

    A name added here takes a stack and everything named for it out of five
    sweeps that exist because leaked runs exhausted the account's role quota,
    and leaves whatever that stack leaks for somebody to remove by hand. So the
    expected count is one, the member is the one registered in
    scripts/tests/gate_exemptions.json, and adding a second is an edit to this
    assertion and to that entry rather than a one-word change in the harness.
    """
    assert cbd.SWEEP_EXEMPT_STACK_NAMES == frozenset({LONG_LIVED_STACK}), (
        f"SWEEP_EXEMPT_STACK_NAMES is {sorted(cbd.SWEEP_EXEMPT_STACK_NAMES)}, not "
        f"the pinned [{LONG_LIVED_STACK!r}]. Each member holds a stack's IAM roles "
        f"for as long as it stands and nothing reaps what it leaks: change this "
        f"pin and its entry in scripts/tests/gate_exemptions.json together, with "
        f"the reason the new stack has to outlive every run"
    )


@pytest.mark.parametrize(
    ("name", "named_for_it"),
    [
        (LONG_LIVED_STACK, True),
        (f"{LONG_LIVED_STACK}-iam", True),
        (f"{LONG_LIVED_STACK}-inputbucket-abc", True),
        (f"{LONG_LIVED_STACK}-iam-PassRolePolicy", True),
        (f"{LONG_LIVED_STACK}-iam-CFServiceRole", True),
        (f"{LONG_LIVED_STACK}{OCR_PROJECT_SUFFIX}", True),
        ("idp-ux-nightlyx", False),
        ("idp-ux-nightly2", False),
        ("idp-ux-nightly2-iam", False),
        (f"idp-ux-nightly2{OCR_PROJECT_SUFFIX}", False),
        ("idp-ux-nightl", False),
        ("xidp-ux-nightly", False),
        (OLD_RUN_STACK, False),
    ],
)
def test_only_the_stack_and_names_under_it_match(cbd, name, named_for_it):
    """The stack itself, `<stack>-…` or `<stack>_…`.

    The hyphen form is the shape `_stack_scoped_policies` uses; the underscore
    form is the stack's BDA-as-OCR project, and no stack name can contain one.
    A bare prefix match would take `idp-ux-nightly2` — another stack, with its
    own roles — out of every sweep along with the one that is meant to stay.
    """
    assert cbd._named_for_exempt_stack(name) is named_for_it


def test_the_ocr_project_named_here_is_the_one_the_stack_creates():
    """The underscore-joined name above is the real one, not a guess at it.

    `Custom::BDAOCRProject` names its project from the main stack's name with
    the function `src/lambda/bda_ocr_project` inlines and is pinned against. Were
    that name to change, every case in this section would go on protecting a
    name the stack no longer creates, while the BDA sweep took the real one.
    """
    from idp_common.bda.bda_ocr import sanitize_ocr_project_name

    assert sanitize_ocr_project_name(LONG_LIVED_STACK) == (
        f"{LONG_LIVED_STACK}{OCR_PROJECT_SUFFIX}"
    ), (
        f"{LONG_LIVED_STACK}'s BDA-as-OCR project is now named "
        f"{sanitize_ocr_project_name(LONG_LIVED_STACK)!r}: check that "
        f"_named_for_exempt_stack still matches it, then update OCR_PROJECT_SUFFIX"
    )


@pytest.mark.parametrize("sweep", _long_lived_sweeps())
def test_the_long_lived_stack_stands_while_an_old_run_beside_it_is_reaped(
    cbd, monkeypatch, capsys, sweep
):
    """Per sweep: nothing of the long-lived stack goes, the old run all goes.

    The role and policy cases pass on deletes alone without the set, since no
    role or policy of the long-lived stack ever matched the run-prefix pattern,
    so it is the printed count that pins those two sweeps' check.
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
        f"{sweep} printed {printed} as the count it left for a long-lived stack"
    )


@pytest.mark.parametrize("lookalike", ["idp-ux-nightlyx", "idp-ux-nightly2"])
@pytest.mark.parametrize("sweep", _long_lived_sweeps())
def test_a_lookalike_of_the_long_lived_stack_gets_no_such_protection(
    cbd, monkeypatch, capsys, sweep, lookalike
):
    """A different stack, so it is reaped like any other leftover.

    The role and policy sweeps are the difference, and it is theirs rather than
    the set's: they only ever delete run-prefixed names, so a lookalike's roles
    and policies are left as they always were. What they must not do is count
    them as the long-lived stack's.
    """
    aws, _ = _install_beside_long_lived(
        cbd, monkeypatch, sweep, [LONG_LIVED_STACK, lookalike]
    )

    findings = _run(cbd, sweep)

    assert findings == [], findings
    reaped = (
        []
        if sweep in ("cleanup_stale_idp_roles", "cleanup_stale_idp_policies")
        else _resources_of(sweep, lookalike)
    )
    assert sorted(aws.deleted) == sorted(reaped), (
        f"{sweep} deleted {aws.deleted} with {lookalike} beside {LONG_LIVED_STACK}; "
        f"expected {reaped}"
    )
    assert _printed_long_lived_count(capsys.readouterr().out) == len(
        _resources_of(sweep, LONG_LIVED_STACK)
    ), f"{sweep} counted {lookalike}'s resources as the long-lived stack's"


@pytest.mark.parametrize(
    "sweep",
    [
        "cleanup_stale_idp_stacks",
        "cleanup_stale_idp_buckets",
        "cleanup_stale_idp_roles",
        "cleanup_stale_idp_policies",
    ],
)
def test_a_listing_of_only_the_long_lived_stack_is_still_the_quiet_path(
    cbd, monkeypatch, capsys, sweep
):
    """Nothing to reap, so the pinned quiet line is printed whole and unchanged.

    The count goes on a line of its own above it. The BDA sweep's quiet line is
    printed only for an empty listing, which one holding the long-lived stack's
    blueprints is not; what it prints instead is pinned in the case below.
    """
    aws, s3 = _install_beside_long_lived(cbd, monkeypatch, sweep, [LONG_LIVED_STACK])

    findings = _run(cbd, sweep)

    assert findings == [], findings
    assert aws.deleted == [] and s3.emptied == []
    out = capsys.readouterr().out
    assert _SWEEP_SPECS[sweep]["quiet_line"] in out.splitlines(), out
    assert cbd.SWEEP_NOT_COMPLETED not in out
    assert _printed_long_lived_count(out) == len(_resources_of(sweep, LONG_LIVED_STACK))


@pytest.mark.parametrize(
    ("statuses", "errors"),
    [
        pytest.param({LONG_LIVED_STACK: "UPDATE_COMPLETE"}, {}, id="up"),
        pytest.param(
            {LONG_LIVED_STACK: "UPDATE_ROLLBACK_FAILED"},
            {},
            id="update-rollback-failed",
        ),
        pytest.param(
            {LONG_LIVED_STACK: "UPDATE_COMPLETE_CLEANUP_IN_PROGRESS"},
            {},
            id="update-cleanup-in-progress",
        ),
        pytest.param(
            {LONG_LIVED_STACK: "UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS"},
            {},
            id="rollback-cleanup-in-progress",
        ),
        pytest.param(
            {}, {LONG_LIVED_STACK: ("Throttling", "Rate exceeded")}, id="throttled"
        ),
    ],
)
def test_the_bda_sweep_never_asks_after_the_long_lived_stack(
    cbd, monkeypatch, capsys, statuses, errors
):
    """Its status cannot make its BDA resources leftovers, so it is not read.

    Three of the statuses sit outside the sweep's active set, and a stack updated
    in place passes through the two cleanup states and can stop in the first, so
    reading any of them as "the run is over" would take its blueprints. The
    first is inside it, and is the case for the BDA-as-OCR project: the first
    three hyphen-separated parts of its name spell no stack, so asking about
    those would take the project while the stack is up. And a `DescribeStacks`
    about it that went unanswered would report the sweep incomplete over
    resources it never deletes. The old run in the same listing is still asked
    about, and still reaped.
    """
    aws, _ = _install_beside_long_lived(
        cbd,
        monkeypatch,
        "cleanup_stale_bda_blueprints",
        [LONG_LIVED_STACK, OLD_RUN_STACK],
        stack_statuses=statuses,
        stack_errors=errors,
    )

    findings = cbd.cleanup_stale_bda_blueprints()

    assert findings == [], findings
    asked = [name for name in aws.described if name.startswith(LONG_LIVED_STACK)]
    assert asked == [], f"the sweep asked CloudFormation about {asked}"
    assert OLD_RUN_STACK in aws.described, aws.described
    assert sorted(aws.deleted) == sorted(
        _resources_of("cleanup_stale_bda_blueprints", OLD_RUN_STACK)
    ), aws.deleted
    assert _printed_long_lived_count(capsys.readouterr().out) == len(
        _resources_of("cleanup_stale_bda_blueprints", LONG_LIVED_STACK)
    )


@pytest.mark.parametrize("value", UNDATEABLE)
@pytest.mark.parametrize(
    "sweep", [s for s in _long_lived_sweeps() if _SWEEP_SPECS[s]["age_gate"]]
)
def test_the_long_lived_stack_is_set_aside_before_the_fail_closed_age_gate(
    cbd, monkeypatch, capsys, sweep, value
):
    """Counted as long-lived where the listing gives it no usable timestamp.

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
    """Passed over before the set is asked, as every nested stack is.

    A nested stack is named `<parent>-<LogicalId>-<suffix>`, so each of the
    long-lived stack's is named for it too. Counted, the main template's nested
    stacks would sit on a line whose other counts are of top-level stacks only.
    The old run's nested stacks are passed over the same way, and its top-level
    stacks are still reaped.
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
    ), "the long-lived stack's nested stacks were counted as left for it"


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
    which the stack is counted as long-lived whatever the marker says. An expired
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
        ("_live_idp_run_prefixes", "client", "cloudformation"),
        ("cleanup_stale_apigw_test_vpcs", "client", "cloudformation"),
        ("cleanup_stale_bda_blueprints", "client", "bedrock-data-automation"),
        ("cleanup_stale_bda_blueprints", "client", "cloudformation"),
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

    Every static assertion in this section compares a NAME. Pointing all fifteen
    sites at a constant rebuilt as `standard`/3 attempts would satisfy every one
    of them while putting the whole throttle class back in front of the handlers,
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
