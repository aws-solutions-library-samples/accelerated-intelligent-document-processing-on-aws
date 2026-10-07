"""Unit tests for delete_apigw_test_vpc — the ENI-blocked test VPC teardown.

A `*-apigw-vpc` stack whose Lambda ENIs outlive it goes DELETE_FAILED, and the
reaper that re-attempts it runs at the head of every pipeline run. Sweeping the
ENIs is the only mechanism that frees the VPC, so what these tests pin is that
the function reaches the sweep cheaply, retries when a retry can plausibly
succeed, and does not spend a 15-minute waiter when the blocker is unchanged.

The last of those is the eleven-week case and the reason the integration stage
stepped from ~56 to ~77 minutes: the stack sat DELETE_FAILED, the reaper issued
a plain delete first, and two waiters later it was DELETE_FAILED again.

⚠️ RetainResources is deliberately NOT used, and that is asserted rather than
left implicit. Retaining the resources that failed means retaining the security
group and the private subnets — children of the VPC — so CloudFormation then
attempts the VPC and DeleteVpc fails with DependencyViolation. If the VPC were
itself retained the stack record would vanish while the VPC survived, and
cleanup_stale_apigw_test_vpcs finds leaks by listing *stacks*, so an orphaned
VPC with no stack is invisible to every reaper here. A DELETE_FAILED stack is
the better of the two states because it is discoverable.

These mock boto3 so they need no AWS.
"""

from pathlib import Path

import pytest

# The registered CFN loader, reused rather than redefined — see
# _load_pipeline_template below for why that matters. conftest.py puts this
# directory on sys.path so the import is collection-order independent.
from test_config_schema_order import _CfnSafeLoader

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PIPELINE_TEMPLATE = _REPO_ROOT / "scripts" / "sdlc" / "cfn" / "codepipeline-s3.yml"


class _FakeCfn:
    """CloudFormation double driven by a scripted sequence of stack statuses."""

    def __init__(self, statuses, outputs=None, fail_waits=0):
        # statuses is consumed one entry per describe_stacks call; the last
        # entry repeats, so a test only spells out the transitions it cares
        # about.
        #
        # fail_waits is how many of the leading delete waiters raise, and it is
        # a count rather than a boolean because 1 is the shape that matters: a
        # first delete that fails and a retry that succeeds. An always-succeeds
        # or always-raises double cannot express that, and a suite built on one
        # can only assert which arguments were sent — which reads like an
        # outcome assertion without being one.
        self._statuses = list(statuses)
        self._outputs = outputs or {}
        self._fail_waits = fail_waits
        self.delete_calls = []
        self.waits = 0

    def describe_stacks(self, StackName):
        status = self._statuses[0]
        if len(self._statuses) > 1:
            self._statuses.pop(0)
        return {
            "Stacks": [
                {
                    "StackStatus": status,
                    "Outputs": [
                        {"OutputKey": k, "OutputValue": v}
                        for k, v in self._outputs.items()
                    ],
                }
            ]
        }

    def delete_stack(self, **kwargs):
        self.delete_calls.append(kwargs)

    def get_waiter(self, name):
        outer = self

        class _Waiter:
            def wait(self, **kwargs):
                outer.waits += 1
                if outer.waits <= outer._fail_waits:
                    raise RuntimeError(
                        "Waiter StackDeleteComplete failed: Max attempts exceeded"
                    )

        return _Waiter()


class _ClientErrorish(Exception):
    """Minimal stand-in carrying botocore's `response` shape."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Ec2:
    """EC2 double. `deny=True` refuses the way a missing grant does."""

    def __init__(self, eni_ids, deny=False):
        self._eni_ids = list(eni_ids)
        self.deleted = []
        self._deny = deny

    def describe_network_interfaces(self, Filters):
        return {
            "NetworkInterfaces": [
                {"NetworkInterfaceId": i, "Status": "available"} for i in self._eni_ids
            ]
        }

    def delete_network_interface(self, NetworkInterfaceId):
        if self._deny:
            raise _ClientErrorish("UnauthorizedOperation")
        self.deleted.append(NetworkInterfaceId)


def _install(cbd, monkeypatch, cfn, ec2=None):
    def _client(name, *a, **k):
        if name == "cloudformation":
            return cfn
        if name == "ec2":
            if ec2 is None:
                raise RuntimeError("no ec2 double installed")
            return ec2
        raise AssertionError(f"unexpected client {name}")

    monkeypatch.setattr(cbd.boto3, "client", _client)
    monkeypatch.setattr(cbd.time, "sleep", lambda *_: None)
    return cfn


# ---------------------------------------------------------------------------
# The eleven-week case: already DELETE_FAILED
# ---------------------------------------------------------------------------


def test_already_failed_stack_skips_the_plain_delete(cbd, monkeypatch):
    # The whole per-run cost was the waiter on a delete that re-attempts
    # resources whose blocker has not moved.
    cfn = _install(
        cbd,
        monkeypatch,
        _FakeCfn(["DELETE_FAILED"], outputs={"VpcId": "vpc-1"}),
        ec2=_Ec2(["eni-1"], deny=True),
    )
    cbd.delete_apigw_test_vpc("idp-0719-012130-apigw-vpc")
    assert cfn.delete_calls == []
    assert cfn.waits == 0


def test_already_failed_and_refused_spends_no_waiter(cbd, monkeypatch, capsys):
    # The one case where skipping is justified: the refusal proves the blocking
    # ENIs are still attached, so a retry re-attempts the same resources.
    cfn = _install(
        cbd,
        monkeypatch,
        _FakeCfn(["DELETE_FAILED"], outputs={"VpcId": "vpc-1"}),
        ec2=_Ec2(["eni-1"], deny=True),
    )
    assert cbd.delete_apigw_test_vpc("idp-0719-012130-apigw-vpc") is False
    assert cfn.waits == 0
    assert "Not spending the waiter" in capsys.readouterr().out


def test_already_failed_with_nothing_left_to_sweep_still_retries(cbd, monkeypatch):
    # An earlier run deleted the ENIs but its own retry failed, because
    # releasing the security-group dependency is eventually consistent. There is
    # nothing to sweep now and the retry is what finishes the job, so it must
    # still be issued. The retry must never be skipped merely because the sweep
    # found nothing: this reaper runs at the head of every build, and a skip
    # here is a no-op on every one of them while the VPC stays leaked.
    cfn = _install(
        cbd,
        monkeypatch,
        _FakeCfn(["DELETE_FAILED"], outputs={"VpcId": "vpc-1"}),
        ec2=_Ec2([], deny=False),
    )
    assert cbd.delete_apigw_test_vpc("idp-0719-012130-apigw-vpc") is True
    assert cfn.delete_calls == [{"StackName": "idp-0719-012130-apigw-vpc"}]


def test_a_non_eni_blocker_is_still_retried_every_run(cbd, monkeypatch):
    # DELETE_FAILED for a reason that was never ENIs — a DeleteNatGateway,
    # ReleaseAddress or DeleteVpcEndpoints failure. The sweep finds nothing
    # because there is nothing of its kind to find, and a per-run retry is what
    # eventually clears a transient.
    cfn = _install(
        cbd,
        monkeypatch,
        _FakeCfn(["DELETE_FAILED"], outputs={"VpcId": "vpc-1"}, fail_waits=1),
        ec2=_Ec2([], deny=False),
    )
    assert cbd.delete_apigw_test_vpc("idp-0719-012130-apigw-vpc") is False
    assert len(cfn.delete_calls) == 1  # attempted, not skipped
    assert cfn.waits == 1


def test_retain_resources_is_never_requested(cbd, monkeypatch):
    # Retaining the failed resources leaves the VPC's own children in place, so
    # the VPC delete that follows fails on DependencyViolation; retaining the
    # VPC too would hide the leak from a reaper that lists stacks. Neither is
    # wanted, so no delete call may carry RetainResources — in any path.
    for statuses, enis, deny in (
        (["DELETE_FAILED"], ["eni-1"], True),
        (["DELETE_FAILED"], ["eni-1"], False),
        (["CREATE_COMPLETE", "DELETE_FAILED"], ["eni-1"], False),
        (["CREATE_COMPLETE", "DELETE_FAILED"], [], False),
    ):
        cfn = _install(
            cbd,
            monkeypatch,
            _FakeCfn(statuses, outputs={"VpcId": "vpc-1"}, fail_waits=1),
            ec2=_Ec2(enis, deny=deny),
        )
        cbd.delete_apigw_test_vpc("idp-0719-012130-apigw-vpc")
        assert all("RetainResources" not in c for c in cfn.delete_calls)


# ---------------------------------------------------------------------------
# The success path the IAM grant unlocks
# ---------------------------------------------------------------------------


def test_a_successful_sweep_deletes_the_enis_then_the_stack(cbd, monkeypatch, capsys):
    # With the grant in place: sweep removes the ENIs, one delete follows, and
    # no permission-gap line is printed.
    ec2 = _Ec2(["eni-1", "eni-2"], deny=False)
    cfn = _install(
        cbd,
        monkeypatch,
        _FakeCfn(["DELETE_FAILED"], outputs={"VpcId": "vpc-1"}),
        ec2=ec2,
    )
    cbd.delete_apigw_test_vpc("idp-0719-012130-apigw-vpc")
    assert ec2.deleted == ["eni-1", "eni-2"]
    assert cfn.delete_calls == [{"StackName": "idp-0719-012130-apigw-vpc"}]
    out = capsys.readouterr().out
    assert "✅ Test VPC deleted" in out
    assert "HARNESS PERMISSION GAP" not in out


def test_healthy_stack_still_gets_a_plain_delete(cbd, monkeypatch):
    # The ordinary path must not regress.
    cfn = _install(cbd, monkeypatch, _FakeCfn(["CREATE_COMPLETE", "DELETE_COMPLETE"]))
    cbd.delete_apigw_test_vpc("idp-0920-120000-apigw-vpc")
    assert cfn.delete_calls == [{"StackName": "idp-0920-120000-apigw-vpc"}]


def test_a_failure_this_run_is_retried_even_when_nothing_was_swept(cbd, monkeypatch):
    # A first delete can fail on a NAT gateway or VPC endpoint still settling,
    # or on throttling. The sweep then legitimately finds nothing, and the
    # retry is the attempt that succeeds — so it must still happen.
    cfn = _install(
        cbd,
        monkeypatch,
        _FakeCfn(
            ["CREATE_COMPLETE", "DELETE_FAILED"],
            outputs={"VpcId": "vpc-1"},
            fail_waits=1,
        ),
        ec2=_Ec2([], deny=False),
    )
    cbd.delete_apigw_test_vpc("idp-0920-120000-apigw-vpc")
    assert len(cfn.delete_calls) == 2
    assert cfn.waits == 2


# ---------------------------------------------------------------------------
# A refused sweep must name itself, and the grant it names must exist
# ---------------------------------------------------------------------------


def test_a_refused_sweep_is_reported_as_a_permission_gap(cbd, monkeypatch, capsys):
    # "swept 0" alone is indistinguishable from having nothing to sweep, which
    # is how this stayed invisible. The refusal must name itself, and it must
    # name the policy to change: the grant reads as present in the template
    # under a condition this account does not meet, so a reader sent to
    # CodeBuildRole's conditional policy finds it there and concludes the
    # deployed stack is merely stale.
    _install(
        cbd,
        monkeypatch,
        _FakeCfn(["DELETE_FAILED"], outputs={"VpcId": "vpc-1"}),
        ec2=_Ec2(["eni-1"], deny=True),
    )
    cbd.delete_apigw_test_vpc("idp-0719-012130-apigw-vpc")
    out = capsys.readouterr().out
    assert "HARNESS PERMISSION GAP" in out
    assert "CodeBuildEC2VPCPolicy" in out
    assert "codepipeline-s3.yml" in out
    assert "DeployInVPC" in out


def test_sweep_reports_denied_separately_from_swept_nothing(cbd, monkeypatch):
    _install(
        cbd,
        monkeypatch,
        _FakeCfn(["DELETE_FAILED"], outputs={"VpcId": "vpc-1"}),
        ec2=_Ec2(["eni-1"], deny=True),
    )
    assert cbd._force_delete_vpc_stack_enis("x") == (0, True)


def test_a_refused_read_does_not_count_as_a_refused_delete(cbd, monkeypatch, capsys):
    # `denied` is the caller's proof that the blocking ENIs are still attached,
    # and it skips the delete on that basis. Only the ENI delete may establish
    # it: a refused DescribeNetworkInterfaces (or DescribeStacks) says nothing
    # about the blocker, so treating it as a refusal would skip the delete on
    # every build while naming a grant that is already present.
    class _RefusingRead:
        def describe_network_interfaces(self, Filters):
            raise _ClientErrorish("UnauthorizedOperation")

    cfn = _install(
        cbd,
        monkeypatch,
        _FakeCfn(["DELETE_FAILED"], outputs={"VpcId": "vpc-1"}),
        ec2=_RefusingRead(),
    )
    assert cbd._force_delete_vpc_stack_enis("x") == (0, False)

    # ...and end to end: the delete is still issued, and no permission-gap line
    # points the reader at the ENI delete grant.
    assert cbd.delete_apigw_test_vpc("idp-0719-012130-apigw-vpc") is True
    assert cfn.delete_calls == [{"StackName": "idp-0719-012130-apigw-vpc"}]
    out = capsys.readouterr().out
    assert "HARNESS PERMISSION GAP" not in out
    assert "ENI sweep could not run" in out


def test_sweep_with_nothing_to_do_is_not_denied(cbd, monkeypatch):
    _install(
        cbd,
        monkeypatch,
        _FakeCfn(["DELETE_FAILED"], outputs={"VpcId": "vpc-1"}),
        ec2=_Ec2([], deny=False),
    )
    assert cbd._force_delete_vpc_stack_enis("x") == (0, False)


def _load_pipeline_template():
    """Parse the pipeline template with the harness's registered CFN loader.

    Deliberately NOT a local loader of its own.
    test_cfn_loader_safety._LOADERS claims to cover "every CFN loader in the
    SDLC harness" and parametrises its unsafe-deserialization assertions over
    that list; a loader defined locally inside this function could not be named
    there even in principle, so adding one would leave that tripwire green while
    its stated scope had quietly become false. conftest.py puts this directory
    on sys.path precisely so sibling modules can share the registered one.
    """
    with open(_PIPELINE_TEMPLATE) as f:
        loader = _CfnSafeLoader(f)
        try:
            return loader.get_single_data()
        finally:
            loader.dispose()


def test_the_policy_the_message_names_actually_grants_the_sweep():
    # The message above tells the next reader that CodeBuildEC2VPCPolicy
    # carries the grant. Nothing else checks that, so an edit dropping the
    # action again would leave every test green while the message insists
    # otherwise — a control that exists and is never consulted.
    template = _load_pipeline_template()
    policy = template["Resources"]["CodeBuildEC2VPCPolicy"]
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    granted = {
        a
        for st in statements
        for a in (st["Action"] if isinstance(st["Action"], list) else [st["Action"]])
    }
    assert "ec2:DeleteNetworkInterface" in granted, (
        "CodeBuildEC2VPCPolicy must grant ec2:DeleteNetworkInterface — the "
        "permission-gap message in delete_apigw_test_vpc sends readers here"
    )


def test_the_grant_is_not_hidden_behind_a_condition():
    # The original defect was not an absent grant but a conditional one: the
    # action sat inside !If [DeployInVPC, ...] on CodeBuildRole, false whenever
    # VpcId is empty, so it read as present while never existing on the role.
    # A grant this reaper depends on must be unconditional.
    template = _load_pipeline_template()
    policy = template["Resources"]["CodeBuildEC2VPCPolicy"]
    assert "Condition" not in policy, (
        "CodeBuildEC2VPCPolicy must not be conditional — the whole defect was "
        "a grant that existed only under a condition this account never meets"
    )
    statements = policy["Properties"]["PolicyDocument"]["Statement"]
    for st in statements:
        assert isinstance(st, dict) and "Action" in st, (
            f"statement is not a plain mapping, so an intrinsic may be gating "
            f"the grant: {st!r}"
        )


# ---------------------------------------------------------------------------
# The summary line a human actually reads
# ---------------------------------------------------------------------------


class _ListOnlyCfn:
    """Just enough CloudFormation to drive the stale-stack sweep."""

    def __init__(self, names):
        self._names = list(names)

    def get_paginator(self, name):
        from datetime import datetime, timedelta, timezone

        old = datetime.now(tz=timezone.utc) - timedelta(days=30)
        summaries = [{"StackName": n, "CreationTime": old} for n in self._names]

        class _P:
            def paginate(self, **kwargs):
                return [{"StackSummaries": summaries}]

        return _P()


def _install_reaper(cbd, monkeypatch, names, outcome):
    monkeypatch.setattr(cbd.boto3, "client", lambda *a, **k: _ListOnlyCfn(names))
    monkeypatch.setattr(cbd, "delete_apigw_test_vpc", lambda n: outcome[n])


def test_a_stack_left_stuck_is_not_counted_as_reaped(cbd, monkeypatch, capsys):
    # The premise of this whole change is that two outcomes reading identically
    # in the log is what hid the leak for eleven weeks. "Reaped 1" printed for a
    # stack this reaper could not delete is that same defect one level up, in
    # the one line a human reads.
    _install_reaper(
        cbd,
        monkeypatch,
        ["idp-0719-012130-apigw-vpc", "idp-0801-100000-apigw-vpc"],
        {"idp-0719-012130-apigw-vpc": False, "idp-0801-100000-apigw-vpc": True},
    )
    cbd.cleanup_stale_apigw_test_vpcs()
    out = capsys.readouterr().out
    assert "✅ Reaped 1 stale apigw test VPC stack(s)" in out
    assert "1 stale apigw test VPC stack(s) left stuck" in out
    assert "idp-0719-012130-apigw-vpc" in out


def test_no_success_line_when_every_stack_is_left_stuck(cbd, monkeypatch, capsys):
    _install_reaper(
        cbd,
        monkeypatch,
        ["idp-0719-012130-apigw-vpc"],
        {"idp-0719-012130-apigw-vpc": False},
    )
    cbd.cleanup_stale_apigw_test_vpcs()
    out = capsys.readouterr().out
    assert "✅ Reaped" not in out
    assert "left stuck" in out


def test_reaper_never_raises_when_cloudformation_errors(cbd, monkeypatch):
    class _Boom:
        def describe_stacks(self, StackName):
            raise RuntimeError("throttled")

        def delete_stack(self, **kwargs):
            raise RuntimeError("throttled")

        def get_waiter(self, name):
            raise RuntimeError("throttled")

    _install(cbd, monkeypatch, _Boom(), ec2=_Ec2([], deny=False))
    # Best effort: must swallow, not propagate.
    cbd.delete_apigw_test_vpc("idp-0719-012130-apigw-vpc")
